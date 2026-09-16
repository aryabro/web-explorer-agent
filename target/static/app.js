(function () {
  const LEDGER_KEY = "night-window:ledger";
  const SESSION_KEY = "night-window:session";
  const FAULT_KEY = "night-window:fault";

  const seed = {
    members: {
      "12345": {
        memberId: "12345",
        name: "Marin Finch",
        status: "Active",
        accounts: [
          { id: "S-4401", product: "Savings", nickname: "Rainy Day", balance: 1842.37 }
        ]
      },
      "54321": {
        memberId: "54321",
        name: "Juniper Vale",
        status: "Active",
        accounts: [
          { id: "S-1190", product: "Savings", nickname: "Nest Egg", balance: 92.14 }
        ]
      },
      "99999": {
        memberId: "99999",
        name: "Restricted Record",
        status: "Restricted",
        accounts: []
      }
    }
  };

  function ensureLedger() {
    if (!sessionStorage.getItem(LEDGER_KEY)) {
      sessionStorage.setItem(LEDGER_KEY, JSON.stringify(seed));
    }
  }

  function ledger() {
    ensureLedger();
    return JSON.parse(sessionStorage.getItem(LEDGER_KEY));
  }

  function saveLedger(value) {
    sessionStorage.setItem(LEDGER_KEY, JSON.stringify(value));
  }

  function session() {
    const raw = sessionStorage.getItem(SESSION_KEY);
    return raw ? JSON.parse(raw) : null;
  }

  function signIn(teller) {
    const salt = crypto.getRandomValues(new Uint32Array(1))[0].toString(36);
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ teller, salt, signedInAt: Date.now() }));
    return salt;
  }

  function signOut() {
    sessionStorage.removeItem(SESSION_KEY);
  }

  function saltFields(root = document) {
    const current = session();
    const salt = current ? current.salt : Math.random().toString(36).slice(2, 8);
    root.querySelectorAll("input, select, button").forEach((el, index) => {
      el.name = `fld_${salt}_${index}_${Math.random().toString(36).slice(2, 5)}`;
    });
  }

  function fault() {
    return sessionStorage.getItem(FAULT_KEY) || "";
  }

  function clearFault() {
    sessionStorage.removeItem(FAULT_KEY);
  }

  function tenantPrefix() {
    return location.pathname.startsWith("/tenant-b") ? "/tenant-b" : "";
  }

  function go(page) {
    window.location.href = `${tenantPrefix()}/${page}.html`;
  }

  function requireSession() {
    if (!session()) {
      go("signin");
      return false;
    }
    return true;
  }

  window.NightWindow = {
    LEDGER_KEY,
    SESSION_KEY,
    FAULT_KEY,
    ensureLedger,
    ledger,
    saveLedger,
    session,
    signIn,
    signOut,
    saltFields,
    fault,
    clearFault,
    go,
    requireSession
  };
  ensureLedger();
})();

