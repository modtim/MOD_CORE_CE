// Dashboard/js/context-menu.js
// Generic right-click context menu

let activeMenu = null;

export function showContextMenu(x, y, items) {
    hideContextMenu();

    const menu = document.createElement('div');
    menu.id = 'context-menu';
    menu.className = 'fixed z-50 bg-white rounded-lg shadow-xl border border-gray-200 py-1 min-w-[180px]';

    items.forEach(item => {
        if (item.divider) {
            const divider = document.createElement('div');
            divider.className = 'border-t border-gray-100 my-1';
            menu.appendChild(divider);
            return;
        }

        const btn = document.createElement('button');
        btn.className = `w-full text-left px-4 py-2 text-sm flex items-center space-x-2 ${
            item.disabled
                ? 'text-gray-400 cursor-not-allowed'
                : 'text-gray-700 hover:bg-gray-100'
        }`;
        btn.disabled = !!item.disabled;

        if (item.icon) {
            const iconSpan = document.createElement('span');
            iconSpan.className = 'w-5 text-center flex-shrink-0';
            iconSpan.innerHTML = item.icon;
            btn.appendChild(iconSpan);
        }

        const label = document.createElement('span');
        label.textContent = item.label;
        btn.appendChild(label);

        if (!item.disabled) {
            btn.addEventListener('click', () => {
                hideContextMenu();
                item.action();
            });
        }

        menu.appendChild(btn);
    });

    document.body.appendChild(menu);

    // Position with viewport bounds checking
    const rect = menu.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - 8;
    const maxY = window.innerHeight - rect.height - 8;
    menu.style.left = `${Math.min(x, maxX)}px`;
    menu.style.top = `${Math.min(y, maxY)}px`;

    activeMenu = menu;

    // Dismiss handlers
    const dismiss = (e) => {
        if (e.type === 'keydown' && e.key !== 'Escape') return;
        hideContextMenu();
        document.removeEventListener('click', dismiss);
        document.removeEventListener('keydown', dismiss);
        document.removeEventListener('scroll', dismiss, true);
    };

    setTimeout(() => {
        document.addEventListener('click', dismiss);
        document.addEventListener('keydown', dismiss);
        document.addEventListener('scroll', dismiss, true);
    }, 0);
}

export function hideContextMenu() {
    if (activeMenu) {
        activeMenu.remove();
        activeMenu = null;
    }
    const existing = document.getElementById('context-menu');
    if (existing) existing.remove();
}
