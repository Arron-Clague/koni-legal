(function () {
    let injected = false;

    function injectMatterForm() {
        if (injected) return;
        if (!window.location.pathname.startsWith('/login')) return;

        const form = document.querySelector('form');
        if (!form) return;

        injected = true;

        const container = document.createElement('div');
        container.id = 'new-matter-container';
        container.style.cssText = 'margin-top:24px;padding-top:16px;border-top:1px solid rgba(0,0,0,0.15);width:100%';

        container.innerHTML = `
            <div style="font-size:13px;color:#888;text-align:center;margin-bottom:12px;">— or create a new matter —</div>
            <div style="margin-bottom:8px">
                <label style="display:block;font-size:12px;margin-bottom:4px;color:#555;font-weight:500">New Matter</label>
                <input id="cl-matter-name" type="text" placeholder="Enter matter name"
                    style="width:100%;box-sizing:border-box;padding:8px 12px;border:1px solid #ccc;border-radius:4px;font-size:14px;outline:none">
            </div>
            <div style="margin-bottom:12px">
                <label style="display:block;font-size:12px;margin-bottom:4px;color:#555;font-weight:500">Password</label>
                <input id="cl-matter-pw" type="password" placeholder="Set a password"
                    style="width:100%;box-sizing:border-box;padding:8px 12px;border:1px solid #ccc;border-radius:4px;font-size:14px;outline:none">
            </div>
            <button id="cl-create-matter" type="button"
                style="width:100%;padding:9px;background:#1976d2;color:#fff;border:none;border-radius:4px;font-size:14px;font-weight:500;cursor:pointer;letter-spacing:0.5px">
                Create New Matter
            </button>
            <div id="cl-matter-msg" style="margin-top:8px;font-size:13px;text-align:center;min-height:18px"></div>
        `;

        form.parentNode.insertBefore(container, form.nextSibling);

        document.getElementById('cl-create-matter').addEventListener('click', async function () {
            const name = document.getElementById('cl-matter-name').value.trim();
            const pw = document.getElementById('cl-matter-pw').value;
            const msg = document.getElementById('cl-matter-msg');

            if (!name || !pw) {
                msg.textContent = 'Please enter both a matter name and password.';
                msg.style.color = '#d32f2f';
                return;
            }

            try {
                const res = await fetch('/api/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ matter: name, password: pw })
                });
                const data = await res.json();
                if (data.success) {
                    msg.textContent = '\u2713 Matter "' + name + '" created. You can now log in.';
                    msg.style.color = '#388e3c';
                    document.getElementById('cl-matter-name').value = '';
                    document.getElementById('cl-matter-pw').value = '';
                } else {
                    msg.textContent = data.error || 'Failed to create matter.';
                    msg.style.color = '#d32f2f';
                }
            } catch (e) {
                msg.textContent = 'Network error. Please try again.';
                msg.style.color = '#d32f2f';
            }
        });
    }

    // Reset injection flag on navigation (SPA route changes)
    let lastPath = window.location.pathname;
    setInterval(function () {
        if (window.location.pathname !== lastPath) {
            lastPath = window.location.pathname;
            injected = false;
            injectMatterForm();
        }
    }, 300);

    // Watch for React rendering the form
    const observer = new MutationObserver(function () {
        if (!injected) injectMatterForm();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });

    window.addEventListener('load', injectMatterForm);
})();

// Fix login button text visibility
(function fixLoginButton() {
    function applyFix() {
        document.querySelectorAll('button[type=submit], form button').forEach(function(btn) {
            var bg = window.getComputedStyle(btn).backgroundColor;
            var color = window.getComputedStyle(btn).color;
            // If text color is dark (rgb values all < 80), force white
            var match = color.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
            if (match) {
                var r = parseInt(match[1]), g = parseInt(match[2]), b = parseInt(match[3]);
                if (r < 80 && g < 80 && b < 80) {
                    btn.style.setProperty('color', '#ffffff', 'important');
                }
            }
            // If color is not set or transparent, force white
            if (color === 'rgba(0, 0, 0, 0)' || color === 'transparent') {
                btn.style.setProperty('color', '#ffffff', 'important');
            }
        });
    }

    var btnObserver = new MutationObserver(applyFix);
    btnObserver.observe(document.documentElement, { childList: true, subtree: true });
    setInterval(applyFix, 500);
})();
