// Moncloa-AI Frontend
// Alpine.js components and HTMX extensions

document.addEventListener('alpine:init', () => {
    // Any global Alpine data can go here
});

// Auto-refresh dashboard status
setInterval(() => {
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    if (dot && txt) {
        fetch('/api/status')
            .then(r => r.json())
            .then(d => {
                const status = d.engine_status;
                txt.textContent = status;
                dot.className = 'w-2 h-2 rounded-full ' +
                    (status === 'running' ? 'bg-green-500' :
                     status === 'paused' ? 'bg-yellow-500' : 'bg-gray-500');
            })
            .catch(() => {});
    }
}, 10000);
