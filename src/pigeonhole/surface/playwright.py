from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Locator,
    Page,
    async_playwright,
)

from pigeonhole.contracts import (
    Checkpoint,
    GeometryTarget,
    Recovery,
    SemanticTarget,
    StructuralTarget,
    TargetBundle,
)
from pigeonhole.surface.base import (
    FrameObservation,
    Observation,
    SurfaceResolutionError,
)
from pigeonhole.surface.perception import (
    assemble_observation,
    control_from_snapshot,
    harvest_target,
)
from pigeonhole.surface.resolution import vote_identities


@dataclass
class LocatorVote:
    locator: Locator
    winners: list[str]
    agreement: int
    weak: bool
    identities: dict[str, str] = field(default_factory=dict)


class PlaywrightSurface:
    """Hybrid DOM/visual surface. CSS details never leave this adapter."""

    def __init__(
        self,
        page: Page,
        context: BrowserContext,
        browser: Browser,
        playwright: Any,
        *,
        skip_frames: Sequence[str] = (),
        cover_frames: Sequence[str] = (),
    ) -> None:
        self.page = page
        self.context = context
        self.browser = browser
        self._playwright = playwright
        self._skip_frames = frozenset(skip_frames)
        self._cover_frames = frozenset(cover_frames)
        self._refs: dict[str, tuple[Frame, int, dict[str, Any]]] = {}
        self._observation_sequence = 0
        self._paused = asyncio.Event()
        self._paused.set()
        self.last_vote: LocatorVote | None = None

    @classmethod
    async def launch(
        cls,
        *,
        headless: bool = True,
        skip_frames: Sequence[str] = (),
        cover_frames: Sequence[str] = (),
    ) -> "PlaywrightSurface":
        playwright = await async_playwright().start()
        launch_options: dict[str, Any] = {"headless": headless}
        configured = Path(playwright.chromium.executable_path)
        if not configured.exists():
            roots = [
                Path(os.getenv("LOCALAPPDATA", "")) / "ms-playwright",
                Path.home() / ".cache" / "ms-playwright",
                Path.home() / "Library" / "Caches" / "ms-playwright",
            ]
            patterns = (
                [
                    "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
                    "chromium-*/chrome-win64/chrome.exe",
                ]
                if headless
                else ["chromium-*/chrome-win64/chrome.exe"]
            )
            fallback = next(
                (
                    match
                    for root in roots
                    for pattern in patterns
                    for match in sorted(root.glob(pattern), reverse=True)
                ),
                None,
            )
            if fallback is not None:
                launch_options["executable_path"] = str(fallback)
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        surface = cls(
            page,
            context,
            browser,
            playwright,
            skip_frames=skip_frames,
            cover_frames=cover_frames,
        )
        context.on("page", surface._adopt_page)
        return surface

    def _adopt_page(self, page: Page) -> None:
        """Follow a real http(s) tab opened by the current page (catalog popups)."""

        def _maybe_switch(frame: Frame) -> None:
            if frame != page.main_frame or page.is_closed():
                return
            if page.opener is not None and page.opener is not self.page:
                return
            if page.url.startswith(("http://", "https://")):
                self.page = page

        page.on("framenavigated", _maybe_switch)

    async def _await_settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            return

    async def _await_interactive(self, *, timeout_ms: int = 15000) -> None:
        """Wait until the live page exposes at least one usable control.

        SPAs often fire `load` before hydrating; discovery must not observe an
        empty shell or the model will invent refs.
        """
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            try:
                ready = await self.page.evaluate(
                    """() => {
                      const body = document.body;
                      if (!body || !(body.innerText || '').trim()) return false;
                      return Boolean(
                        body.querySelector(
                          'input,textarea,select,button,a,[role="button"],[role="textbox"],[role="combobox"]'
                        )
                      );
                    }"""
                )
            except Exception:
                ready = False
            if ready:
                await self._await_settle()
                return
            await asyncio.sleep(0.2)

    async def close(self) -> None:
        await self.context.close()
        await self.browser.close()
        await self._playwright.stop()

    @staticmethod
    def _frame_key(frame: Frame) -> str:
        if frame.name:
            return frame.name
        # An unnamed main frame must not be keyed by URL, or every navigation
        # would invalidate recorded targets on single-frame applications.
        if frame.parent_frame is None:
            return "main"
        path = urlparse(frame.url).path
        return path or "root"

    def _find_frame(self, key: str) -> Frame | None:
        for frame in self.page.frames:
            if self._frame_key(frame) == key:
                return frame
        return None

    async def observe(self) -> Observation:
        self._refs.clear()
        self._observation_sequence += 1
        observation_id = f"o{self._observation_sequence}"
        controls = []
        frames: list[FrameObservation] = []
        all_text: list[str] = []
        dialogs: list[str] = []
        alerts: list[str] = []
        busy = False
        truncated = False
        ordinal = 0
        script = """
        () => {
          const candidates = [...document.querySelectorAll(
            'input,textarea,select,button,a,strong,span,b,h1,h2,h3,[role="status"]'
          )];
          const visible = el => {
            const s = getComputedStyle(el), r = el.getBoundingClientRect();
            return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
          };
          const role = el => el.getAttribute('role') || ({
            BUTTON: 'button', A: 'link', SELECT: 'combobox', TEXTAREA: 'textbox'
          }[el.tagName] || (el.tagName === 'INPUT'
            ? (['button','submit'].includes(el.type) ? 'button' : 'textbox') : null));
          const name = el => el.getAttribute('aria-label') ||
            (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText) ||
            (['BUTTON','A'].includes(el.tagName) ? el.innerText.trim() : '') || null;
          const testId = el => el.getAttribute('data-automation-id') ||
            el.getAttribute('data-testid') || el.getAttribute('data-test-id') || null;
          return {
            bodyText: document.body?.innerText || '',
            candidateCount: candidates.length,
            dialogs: [...document.querySelectorAll('dialog[open],[role="dialog"]')]
              .filter(visible).map(el => (el.innerText || '').trim().slice(0, 500)).slice(0, 5),
            alerts: [...document.querySelectorAll('[role="alert"]')]
              .filter(visible).map(el => (el.innerText || '').trim().slice(0, 500)).slice(0, 5),
            busy: Boolean(document.querySelector('[aria-busy="true"]')),
            controls: candidates.map((el, domIndex) => {
              if (!visible(el)) return null;
              const r = el.getBoundingClientRect();
              const tr = el.closest('tr');
              const cell = el.closest('td,th');
              const table = el.closest('table');
              const tables = [...document.querySelectorAll('table')];
              const rows = table ? [...table.querySelectorAll('tr')] : [];
              const cells = tr ? [...tr.children].filter(x => ['TD','TH'].includes(x.tagName)) : [];
              const same = [...document.querySelectorAll(el.tagName.toLowerCase())];
              return {
                domIndex,
                tag: el.tagName.toLowerCase(),
                inputType: el.getAttribute('type'),
                role: role(el),
                name: name(el),
                testId: testId(el),
                hasValue: Boolean(el.value),
                interactive: ['INPUT','TEXTAREA','SELECT','BUTTON','A'].includes(el.tagName) ||
                  ['button','textbox','combobox','checkbox','radio','link'].includes(role(el)),
                disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
                checked: ['checkbox','radio'].includes(el.type) ? Boolean(el.checked) : null,
                required: Boolean(el.required) || el.getAttribute('aria-required') === 'true',
                readOnly: Boolean(el.readOnly),
                expanded: el.hasAttribute('aria-expanded') ? el.getAttribute('aria-expanded') === 'true' : null,
                visibleText: (el.tagName === 'INPUT' ? '' : (el.innerText || el.value || '')).trim(),
                nearbyText: (tr?.innerText || el.parentElement?.innerText || '').trim().slice(0, 240),
                box: {x:r.x,y:r.y,width:r.width,height:r.height},
                tableIndex: table ? tables.indexOf(table) : null,
                rowIndex: tr ? rows.indexOf(tr) : null,
                cellIndex: cell ? cells.indexOf(cell) : null,
                typeIndex: same.indexOf(el)
              };
            }).filter(Boolean).slice(0, 160)
          };
        }
        """
        for frame in self.page.frames:
            frame_key = self._frame_key(frame)
            if frame_key in self._skip_frames:
                continue
            try:
                state = await frame.evaluate(script)
            except Exception:
                continue
            normalized_text = " ".join(state["bodyText"].split())
            body_text = normalized_text[:12000]
            truncated = truncated or len(normalized_text) > len(body_text)
            truncated = truncated or state["candidateCount"] > len(state["controls"])
            dialogs.extend(f"[{frame_key}] {text}" for text in state["dialogs"] if text)
            alerts.extend(f"[{frame_key}] {text}" for text in state["alerts"] if text)
            busy = busy or bool(state["busy"])
            if body_text:
                all_text.append(f"[{frame_key}] {body_text}")
            frame_refs: list[str] = []
            for item in state["controls"]:
                ref = f"{observation_id}:c{ordinal}"
                ordinal += 1
                self._refs[ref] = (frame, item["domIndex"], item)
                frame_refs.append(ref)
                controls.append(control_from_snapshot(ref, frame_key, item))
            frames.append(
                FrameObservation(
                    key=frame_key,
                    url=frame.url,
                    visible_text=body_text,
                    control_refs=frame_refs,
                )
            )
        visible_text = "\n".join(all_text)
        return assemble_observation(
            observation_id=observation_id,
            url=self.page.url,
            title=await self.page.title(),
            visible_text=visible_text,
            controls=controls,
            frames=frames,
            dialogs=dialogs,
            alerts=alerts,
            busy=busy,
            truncated=truncated,
        )

    def _ref_locator(self, ref: str) -> Locator:
        try:
            frame, index, _ = self._refs[ref]
        except KeyError as exc:
            raise SurfaceResolutionError(
                f"observation ref expired or unknown: {ref}"
            ) from exc
        return frame.locator(
            'input,textarea,select,button,a,strong,span,b,h1,h2,h3,[role="status"]'
        ).nth(index)

    async def act(self, action: str, ref: str | None = None, value: Any = None) -> Any:
        await self._paused.wait()
        if action == "navigate":
            await self.page.goto(str(value), wait_until="load")
            await self._await_interactive()
            return None
        if action == "wait":
            await asyncio.sleep(float(value or 0.25))
            return None
        if ref is None:
            raise SurfaceResolutionError(f"{action} requires a control ref")
        locator = self._ref_locator(ref)
        if action in {"click", "dismiss"}:
            await locator.click()
            await self._await_settle()
            return None
        if action == "type":
            await locator.fill(str(value))
            return None
        if action == "select":
            await locator.select_option(label=str(value))
            return None
        if action == "extract":
            return (await locator.inner_text()).strip()
        raise ValueError(f"unsupported action: {action}")

    async def harvest(self, ref: str) -> TargetBundle:
        try:
            frame, _, item = self._refs[ref]
        except KeyError as exc:
            raise SurfaceResolutionError(f"cannot harvest unknown ref: {ref}") from exc
        frame_key = self._frame_key(frame)
        return harvest_target(frame_key, item)

    @staticmethod
    def _semantic_locators(frame: Frame, strategy: SemanticTarget) -> list[Locator]:
        locators: list[Locator] = []
        if strategy.test_id:
            locators.append(frame.locator(f"[data-automation-id='{strategy.test_id}']"))
            locators.append(frame.locator(f"[data-testid='{strategy.test_id}']"))
            locators.append(frame.locator(f"[data-test-id='{strategy.test_id}']"))
        if strategy.role and strategy.name:
            locators.append(
                frame.get_by_role(strategy.role, name=strategy.name, exact=True)
            )
        if strategy.adjacent_text:
            locators.append(frame.get_by_label(strategy.adjacent_text, exact=True))
            if strategy.element_type in {"input", "select", "textarea"}:
                locators.append(
                    frame.get_by_text(strategy.adjacent_text, exact=True).locator(
                        f"xpath=following::{strategy.element_type}[1]"
                    )
                )
        if strategy.name and strategy.element_type in {
            "button",
            "a",
            "strong",
            "span",
            "b",
            "h1",
            "h2",
            "h3",
        }:
            locators.append(frame.get_by_text(strategy.name, exact=True))
        return locators

    async def _unique_visible(self, locator: Locator) -> Locator | None:
        try:
            if await locator.count() == 1 and await locator.is_visible():
                return locator
        except Exception:
            return None
        return None

    async def _stamp(self, locator: Locator) -> str | None:
        try:
            return await locator.evaluate(
                """el => {
                  if (!el.__pigeonholeId) {
                    el.__pigeonholeId = Math.random().toString(36).slice(2);
                  }
                  return el.__pigeonholeId;
                }"""
            )
        except Exception:
            return None

    async def _resolve_strategy(
        self,
        strategy: SemanticTarget | StructuralTarget | GeometryTarget,
        reasons: list[str],
    ) -> Locator | None:
        frame = self._find_frame(strategy.frame)
        if frame is None:
            reasons.append(f"{strategy.kind}: frame missing")
            return None
        if isinstance(strategy, SemanticTarget):
            forms = self._semantic_locators(frame, strategy)
            if not forms:
                reasons.append("semantic: insufficient identity")
                return None
            for form in forms:
                unique = await self._unique_visible(form)
                if unique is not None:
                    return unique
            reasons.append("semantic: no unique match")
            return None
        if isinstance(strategy, StructuralTarget):
            if strategy.table_index is not None and strategy.row_index is not None:
                locator = (
                    frame.locator("table")
                    .nth(strategy.table_index)
                    .locator("tr")
                    .nth(strategy.row_index)
                )
                if strategy.cell_index is not None:
                    locator = locator.locator("td,th").nth(strategy.cell_index)
                locator = locator.locator(strategy.element_type)
            else:
                locator = frame.locator(strategy.element_type).nth(strategy.type_index)
        else:
            candidates = frame.locator(strategy.element_type)
            count = await candidates.count()
            if not count:
                reasons.append("geometry: no candidates")
                return None
            expected = strategy.expected_box
            best_index, best_distance = -1, float("inf")
            for index in range(count):
                box = await candidates.nth(index).bounding_box()
                if not box:
                    continue
                distance = abs(box["x"] - expected.x) + abs(box["y"] - expected.y)
                if distance < best_distance:
                    best_index, best_distance = index, distance
            if best_index < 0:
                reasons.append("geometry: no visible candidate")
                return None
            locator = candidates.nth(best_index)
        unique = await self._unique_visible(locator)
        if unique is None:
            reasons.append(f"{strategy.kind}: ambiguous or hidden")
        return unique

    async def resolve_vote(self, target: TargetBundle) -> LocatorVote:
        reasons: list[str] = []
        found: list[tuple[str, Locator, str]] = []
        for strategy in target.strategies:
            locator = await self._resolve_strategy(strategy, reasons)
            if locator is None:
                continue
            identity = await self._stamp(locator)
            if identity is None:
                reasons.append(f"{strategy.kind}: could not identify element")
                continue
            found.append((strategy.kind, locator, identity))
        if not found:
            self.last_vote = None
            raise SurfaceResolutionError("; ".join(reasons) or "no strategies resolved")
        try:
            identity_vote = vote_identities(
                [(kind, identity) for kind, _, identity in found]
            )
        except SurfaceResolutionError:
            self.last_vote = None
            raise
        locator = next(
            loc for kind, loc, identity in found if identity == identity_vote.identity
        )
        vote = LocatorVote(
            locator=locator,
            winners=list(identity_vote.winners),
            agreement=identity_vote.agreement,
            weak=identity_vote.weak,
            identities=dict(identity_vote.identities),
        )
        self.last_vote = vote
        return vote

    async def resolve(self, target: TargetBundle) -> Locator:
        return (await self.resolve_vote(target)).locator

    async def act_target(
        self, action: str, target: TargetBundle, value: Any = None
    ) -> Any:
        await self._paused.wait()
        locator = (await self.resolve_vote(target)).locator
        if action in {"click", "dismiss"}:
            await locator.click()
            await self._await_settle()
            return None
        if action == "type":
            await locator.fill(str(value))
            return None
        if action == "select":
            await locator.select_option(label=str(value))
            return None
        if action == "extract":
            return (await locator.inner_text()).strip()
        raise ValueError(f"unsupported target action: {action}")

    async def recover(self, recovery: Recovery) -> bool:
        if not await self.checkpoint_visible(recovery.trigger):
            return False
        if recovery.strategy == "dismiss" and recovery.action_text:
            for frame in self.page.frames:
                button = frame.get_by_text(recovery.action_text, exact=True)
                count = await button.count()
                for index in range(count):
                    candidate = button.nth(index)
                    if await candidate.is_visible():
                        await candidate.click()
                        await self._await_settle()
                        return True
            return False
        if recovery.strategy == "wait":
            await asyncio.sleep(recovery.timeout_ms / 1000)
            return True
        return False

    async def checkpoint_visible(self, checkpoint: Checkpoint) -> bool:
        if checkpoint.kind == "url_contains":
            return checkpoint.expected in self.page.url
        frames = (
            [self._find_frame(checkpoint.frame)]
            if checkpoint.frame
            else self.page.frames
        )
        for frame in (item for item in frames if item is not None):
            try:
                if await frame.get_by_text(checkpoint.expected, exact=False).count():
                    return True
            except Exception:
                pass
        return False

    async def checkpoint(self, checkpoint: Checkpoint) -> bool:
        deadline = asyncio.get_running_loop().time() + checkpoint.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            if await self.checkpoint_visible(checkpoint):
                return True
            await asyncio.sleep(0.1)
        return False

    async def screenshot(
        self, path: str, redact_values: list[str] | None = None
    ) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        values = [value for value in (redact_values or []) if value]
        script = """
        (values) => {
          const changed = [];
          const mask = text => values.reduce(
            (result, value) => result.split(value).join('[REDACTED]'), text);
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          let node;
          while ((node = walker.nextNode())) {
            const next = mask(node.nodeValue || '');
            if (next !== node.nodeValue) {
              changed.push({kind:'text', node, value:node.nodeValue});
              node.nodeValue = next;
            }
          }
          document.querySelectorAll('input,textarea').forEach(el => {
            const next = mask(el.value || '');
            if (next !== el.value) {
              changed.push({kind:'value', node:el, value:el.value});
              el.value = next;
            }
          });
          window.__pigeonholeRestore = () => changed.forEach(item => {
            if (item.kind === 'text') item.node.nodeValue = item.value;
            else item.node.value = item.value;
          });
        }
        """
        try:
            for frame in self.page.frames:
                try:
                    if self._frame_key(frame) in self._cover_frames:
                        await frame.evaluate(
                            """() => {
                              const cover = document.createElement('div');
                              cover.id = 'pigeonhole-oracle-cover';
                              cover.textContent = 'INTERNAL ORACLE REDACTED';
                              Object.assign(cover.style, {
                                position: 'fixed', inset: '0', zIndex: '2147483647',
                                background: '#d9d3ad', color: '#263d18',
                                padding: '20px', fontFamily: 'monospace'
                              });
                              document.documentElement.appendChild(cover);
                              window.__pigeonholeRestoreDrawer = () => {
                                cover.remove();
                              };
                            }"""
                        )
                        continue
                    await frame.evaluate(script, values)
                except Exception:
                    pass
            await self.page.screenshot(path=str(destination), full_page=False)
        finally:
            for frame in self.page.frames:
                try:
                    if self._frame_key(frame) in self._cover_frames:
                        await frame.evaluate(
                            "() => window.__pigeonholeRestoreDrawer?.()"
                        )
                    else:
                        await frame.evaluate("() => window.__pigeonholeRestore?.()")
                except Exception:
                    pass

    async def pause(self) -> None:
        self._paused.clear()

    async def resume(self) -> None:
        self._paused.set()

    async def storage_snapshot(self) -> dict[str, Any]:
        return await self.page.evaluate(
            """() => Object.fromEntries(
              Object.keys(sessionStorage).map(key => [key, sessionStorage.getItem(key)])
            )"""
        )

    @staticmethod
    def _capture_script() -> str:
        return """
        (() => {
          if (window.__pigeonholeCaptureInstalled) return;
          window.__pigeonholeCaptureInstalled = true;
          const save = event => {
            if (sessionStorage.getItem('pigeonhole:human-control') !== '1') return;
            const rows = JSON.parse(sessionStorage.getItem('pigeonhole:human-events') || '[]');
            rows.push({
              at: new Date().toISOString(),
              event: event.type,
              tag: event.target?.tagName?.toLowerCase() || 'unknown',
              input_type: event.target?.getAttribute?.('type') || null,
              visible_text: event.type === 'click'
                ? (event.target?.innerText || '').trim().slice(0, 80) : null
            });
            sessionStorage.setItem('pigeonhole:human-events', JSON.stringify(rows));
          };
          document.addEventListener('click', save, true);
          document.addEventListener('change', save, true);
        })()
        """

    async def start_human_capture(self) -> None:
        await self.context.add_init_script(self._capture_script())
        await self.page.evaluate(
            """() => {
              sessionStorage.setItem('pigeonhole:human-events', '[]');
              sessionStorage.setItem('pigeonhole:human-control', '1');
            }"""
        )
        for frame in self.page.frames:
            try:
                await frame.evaluate(self._capture_script())
            except Exception:
                pass

    async def stop_human_capture(self) -> list[dict[str, Any]]:
        rows = await self.page.evaluate(
            """() => {
              sessionStorage.removeItem('pigeonhole:human-control');
              return JSON.parse(sessionStorage.getItem('pigeonhole:human-events') || '[]');
            }"""
        )
        return rows
