"""Deterministic test models.

These exercise the real browser loop but are never valid submission evidence.
"""

from __future__ import annotations

import json

from pigeonhole.contracts import Risk
from pigeonhole.discovery import Decision


class ScriptedModel:
    name = "fixture-model-not-submission-evidence"

    def __init__(self, flow: str = "read") -> None:
        self.flow = flow
        self.turn = 0

    async def decide(self, prompt: str) -> Decision:
        controls = json.loads(prompt)["observation"]["controls"]

        def ref(text: str, element_type: str) -> str:
            for control in controls:
                haystack = " ".join(
                    [
                        control["nearby_text"],
                        control["visible_text"],
                        control.get("accessible_name") or "",
                    ]
                )
                if text in haystack and control["element_type"] == element_type:
                    return control["ref"]
            raise RuntimeError(f"fixture could not find {element_type} near {text!r}")

        common = [
            Decision(
                kind="act",
                intent="Enter the runtime employee identifier",
                action="type",
                ref=ref("Employee ID", "input"),
                input_name="operator_id",
            )
            if self.turn == 0
            else None,
            Decision(
                kind="act",
                intent="Enter the runtime-only security PIN",
                action="type",
                ref=ref("Security PIN", "input"),
                input_name="pin",
            )
            if self.turn == 1
            else None,
            Decision(
                kind="act",
                intent="Sign in to the test bank console",
                action="click",
                ref=ref("Sign in", "button"),
                checkpoint_text="Member search",
            )
            if self.turn == 2
            else None,
            Decision(
                kind="act",
                intent="Enter the requested member number",
                action="type",
                ref=ref("Member number", "input"),
                input_name="member_id",
            )
            if self.turn == 3
            else None,
            Decision(
                kind="act",
                intent="Open the matching member profile",
                action="click",
                ref=ref("Search members", "button"),
                checkpoint_text="MEMBER PROFILE READY",
            )
            if self.turn == 4
            else None,
        ]
        decision = common[self.turn] if self.turn < len(common) else None
        if decision is None:
            decision = self._flow_decision(ref)
        self.turn += 1
        return decision

    def _flow_decision(self, ref) -> Decision:
        if self.flow == "read":
            if self.turn == 5:
                return Decision(
                    kind="act",
                    intent="Read the visible current savings balance",
                    action="extract",
                    ref=ref("Current savings", "strong"),
                    output="savings_balance",
                )
            return Decision(kind="done", intent="The savings balance was extracted")

        if self.turn == 5:
            return Decision(
                kind="act",
                intent="Open the new savings account form",
                action="click",
                ref=ref("Open savings account", "button"),
                checkpoint_text="New savings account",
            )
        if self.turn == 6:
            return Decision(
                kind="act",
                intent="Select the requested savings product",
                action="select",
                ref=ref("Product", "select"),
                input_name="product",
            )
        if self.turn == 7:
            return Decision(
                kind="act",
                intent="Enter the requested account nickname",
                action="type",
                ref=ref("Nickname", "input"),
                input_name="nickname",
            )
        if self.turn == 8:
            return Decision(
                kind="act",
                intent="Enter the requested opening deposit",
                action="type",
                ref=ref("Opening deposit", "input"),
                input_name="opening_deposit",
            )
        if self.turn == 9:
            return Decision(
                kind="act",
                intent="Review the prepared account",
                action="click",
                ref=ref("Review account", "button"),
                checkpoint_text="Review new account",
            )
        if self.turn == 10:
            return Decision(
                kind="act",
                intent="Confirm creation of the reviewed account",
                action="click",
                ref=ref("Confirm and create account", "button"),
                checkpoint_text="ACCOUNT CREATED",
                risk=Risk.MUTATING,
            )
        if self.turn == 11:
            return Decision(
                kind="act",
                intent="Read the visible new account identifier",
                action="extract",
                ref=ref("New account", "span"),
                output="account_id",
            )
        return Decision(kind="done", intent="The new account confirmation was reached")
