// R-167 — page-level handlers for the standalone error pages (500/404/
// maintenance). Only the elements that exist on the current page get a
// listener, so one shared file serves all three.
document.addEventListener('DOMContentLoaded', () => {
    const retry = document.getElementById('retry-btn');
    if (retry) retry.addEventListener('click', () => location.reload());
});
