// incubator — WebSocket live updates
(function() {
    let ws = null;
    let reconnectDelay = 1000;

    function connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${location.host}/ws/events`);

        ws.onopen = function() {
            reconnectDelay = 1000;
            updateConnectionDot(true);
        };

        ws.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                // Dispatch as custom event so page-specific scripts can listen
                window.dispatchEvent(new CustomEvent('incubator:event', { detail: data }));
                handleGlobalEvent(data);
            } catch (e) {
                console.error('[incubator] parse error:', e);
            }
        };

        ws.onclose = function() {
            updateConnectionDot(false);
            setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
        };
    }

    function updateConnectionDot(connected) {
        // Optional: add a tiny connection indicator to the nav
    }

    function handleGlobalEvent(data) {
        // Live feed on home page
        const feedEl = document.getElementById('live-feed');
        const entriesEl = document.getElementById('feed-entries');
        if (feedEl && entriesEl && data.type === 'activity') {
            feedEl.classList.remove('hidden');
            const entry = document.createElement('div');
            entry.className = 'card-flat px-4 py-2.5 flex items-start gap-3 animate-in';
            entry.innerHTML = `
                <span class="feed-dot ${data.kind || 'info'}"></span>
                <div class="flex-1 min-w-0">
                    <span class="text-[0.8rem] text-[#544d43]">${escapeHtml(data.message)}</span>
                    ${data.idea_id ? `<span class="text-[0.7rem] text-[#b0a898] ml-2">${escapeHtml(data.idea_id)}</span>` : ''}
                </div>
                <span class="text-[0.68rem] text-[#b0a898] font-mono flex-shrink-0">${formatTime(data.timestamp)}</span>
            `;
            entriesEl.prepend(entry);
            // Keep only last 20 entries
            while (entriesEl.children.length > 20) {
                entriesEl.removeChild(entriesEl.lastChild);
            }
        }

        // Refresh home page cards on phase transitions
        if (data.type === 'phase_transition' || data.type === 'idea_created') {
            const homeCards = document.querySelector('[data-page="home"]');
            if (homeCards || location.pathname === '/') {
                setTimeout(() => location.reload(), 600);
            }
        }
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function formatTime(iso) {
        if (!iso) return '';
        try {
            const d = new Date(iso);
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch {
            return '';
        }
    }

    // Connect on load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', connect);
    } else {
        connect();
    }
})();
