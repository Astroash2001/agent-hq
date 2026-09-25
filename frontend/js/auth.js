/**
 * The API token, for a server that requires one.
 *
 * A deployed office requires `AGENTHQ_API_TOKEN` on every /api/ route but the
 * health check, and on the /ws socket (backend/api_guard.py). This keeps the
 * UI working against such a server without touching each call site: same-origin
 * /api/ requests get the token as a header, the socket gets it as ?token=
 * (a browser cannot set WebSocket headers), and a 401 asks for it once.
 *
 * A local server with no token configured never answers 401, so nothing here
 * is ever visible there.
 *
 * The token is kept in this browser's localStorage. Anyone with access to this
 * browser profile can read it, which is the same trust a saved password has.
 *
 * Every request also carries this browser's session id (X-Session-Id, or
 * ?session= on the socket). The server shows each session only the missions,
 * events and attachments it started, so two people sharing one token do not
 * see each other's work (backend/sessions.py). The id is random, minted once
 * and kept in localStorage: clearing site data starts a new, empty history.
 */
(function () {
    const KEY = 'agenthq_api_token';
    const SESSION_KEY = 'agenthq_session_id';
    let asked = false;

    // 32 hex characters: inside the server's [A-Za-z0-9_-]{16,64} shape.
    function mintSession() {
        const bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }

    // Read once per page. When storage is unavailable (private window), the id
    // lasts this page only, so a reload starts a fresh session.
    const sessionId = (function () {
        try {
            let id = localStorage.getItem(SESSION_KEY);
            if (!id || !/^[A-Za-z0-9_-]{16,64}$/.test(id)) {
                id = mintSession();
                localStorage.setItem(SESSION_KEY, id);
            }
            return id;
        } catch (e) {
            return mintSession();
        }
    })();

    function get() {
        try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
    }

    function set(token) {
        try {
            if (token) localStorage.setItem(KEY, token);
            else localStorage.removeItem(KEY);
        } catch (e) { /* storage unavailable: the token lasts this page only */ }
        memoryToken = token || '';
    }

    let memoryToken = get();

    function current() {
        return get() || memoryToken;
    }

    /** Ask for the token. Returns true when one was entered. */
    function ask(reason) {
        const entered = window.prompt(
            (reason || 'This AgentHQ server requires an API token.') +
            '\n\nEnter the value of AGENTHQ_API_TOKEN:');
        if (entered && entered.trim()) {
            set(entered.trim());
            return true;
        }
        return false;
    }

    function isApi(input) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.startsWith('/api/')) return true;
        try {
            const parsed = new URL(url, window.location.href);
            return parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/');
        } catch (e) {
            return false;
        }
    }

    function withToken(init) {
        const token = current();
        const next = Object.assign({}, init || {});
        const headers = new Headers((init && init.headers) || {});
        // Always sent: a server with no token still separates sessions.
        headers.set('X-Session-Id', sessionId);
        if (token) headers.set('Authorization', `Bearer ${token}`);
        next.headers = headers;
        return next;
    }

    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
        if (!isApi(input)) return nativeFetch(input, init);
        const response = await nativeFetch(input, withToken(init));
        // Ask once per page. A wrong token entered twice is a person to talk
        // to, not a loop of dialogs.
        if (response.status === 401 && !asked) {
            asked = true;
            if (ask(current() ? 'The saved API token was rejected.' : '')) {
                return nativeFetch(input, withToken(init));
            }
        }
        return response;
    };

    window.AgentAuth = {
        token: current,
        session: () => sessionId,
        set,
        clear: () => set(''),
        /** `wsUrl` with the session id, and the token when there is one. */
        socketUrl(wsUrl) {
            const token = current();
            let url = `${wsUrl}${wsUrl.includes('?') ? '&' : '?'}session=${encodeURIComponent(sessionId)}`;
            if (token) url += `&token=${encodeURIComponent(token)}`;
            return url;
        },
        /** Called when the socket is refused (close code 1008). */
        askAfterSocketRefusal() {
            if (asked) return false;
            asked = true;
            return ask(current() ? 'The saved API token was rejected.' : '');
        },
    };
})();
