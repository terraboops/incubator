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

        // Live DOM patch on idea updates (projection diff)
        if (data.type === 'idea_update' && location.pathname === '/') {
            const card = document.querySelector(`a[href="/ideas/${data.idea_id}"]`);
            if (card) {
                // Patch badge
                const badge = card.querySelector('.badge');
                if (badge && data.phase) {
                    const label = data.phase.replace(/_/g, ' ');
                    badge.textContent = label;
                    badge.className = badge.className.replace(/badge-\w+/, 'badge-' + data.phase.replace('_review', ''));
                }
                // Patch cost
                const costEl = card.querySelector('.font-mono');
                if (costEl && data.total_cost_usd !== undefined) {
                    costEl.textContent = '$' + data.total_cost_usd.toFixed(2);
                }
            } else {
                // New idea — reload to show it
                setTimeout(() => location.reload(), 600);
            }
        }

        // Refresh home page on phase transitions or new ideas (fallback)
        if (data.type === 'phase_transition' || data.type === 'idea_created') {
            if (location.pathname === '/') {
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

    // Wire failed-role retry forms: POST as JSON so the API returns proper
    // status codes, then reload on success.
    function wireRetryForms() {
        document.querySelectorAll('form[data-retry-form]').forEach((form) => {
            if (form.dataset.retryWired) return;
            form.dataset.retryWired = '1';
            form.addEventListener('submit', async (ev) => {
                ev.preventDefault();
                const btn = form.querySelector('button[type=submit]');
                const original = btn ? btn.textContent : '';
                if (btn) {
                    btn.disabled = true;
                    btn.textContent = 'Retrying…';
                }
                try {
                    const resp = await fetch(form.action, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ actor: 'human:web' }),
                    });
                    if (resp.ok) {
                        window.location.reload();
                    } else {
                        const body = await resp.json().catch(() => ({}));
                        alert('Retry failed: ' + (body.error || resp.statusText));
                        if (btn) {
                            btn.disabled = false;
                            btn.textContent = original;
                        }
                    }
                } catch (e) {
                    alert('Retry failed: ' + e.message);
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = original;
                    }
                }
            });
        });
    }

    // Add-stage affordance on the idea detail page.
    function wireAddStageButton() {
        const btn = document.getElementById('add-custom-stage');
        if (!btn || btn.dataset.wired) return;
        btn.dataset.wired = '1';
        btn.addEventListener('click', async () => {
            const ideaMatch = location.pathname.match(/^\/ideas\/([^/]+)/);
            if (!ideaMatch) return;
            const ideaId = ideaMatch[1];
            const name = prompt(
                'New stage name (lowercase, 1-40 chars, [a-z0-9_-]).\n'
                + 'Example: hotfix, v1, v2, post-launch'
            );
            if (!name) return;
            try {
                const resp = await fetch(`/ideas/${ideaId}/stages`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name.trim(), actor: 'web' }),
                });
                if (resp.ok) {
                    window.location.reload();
                } else {
                    const body = await resp.json().catch(() => ({}));
                    alert('Add stage failed: ' + (body.error || resp.statusText));
                }
            } catch (e) {
                alert('Add stage failed: ' + e.message);
            }
        });
    }

    // Connect on load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            connect();
            wireRetryForms();
            wireAddStageButton();
        });
    } else {
        connect();
        wireRetryForms();
        wireAddStageButton();
    }
})();
