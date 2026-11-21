// collision-resolve.js
// Shared modal for the structured 409 the API returns on copy / move
// / rename name collisions. Resolves the user's choice into one of:
//   { action: "rename", new_name: string }
//   { action: "replace" }
//   null  (cancelled)
//
// The 409 payload shape (see CORE/API/routers/cogs.py
// `_name_collision_409`):
//   {
//     code: "name_collision",
//     message: string,
//     existing: { id, name, cog_type, modified_at, size_bytes },
//     target_folder: { id, name },
//     incoming_name: string,
//   }

const MODAL_ID = "collision-resolve-modal";

function escapeHtml(s) {
    return String(s ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function ensureModal() {
    let modal = document.getElementById(MODAL_ID);
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = MODAL_ID;
    modal.className =
        "fixed inset-0 bg-black bg-opacity-50 hidden items-center justify-center z-50";
    modal.innerHTML = `
        <div class="bg-white rounded-lg shadow-xl max-w-md w-full mx-4">
            <div class="px-6 py-4 border-b border-gray-200">
                <h3 class="text-lg font-semibold text-gray-900" data-cr-title>Name conflict</h3>
                <p class="text-sm text-gray-500 mt-1" data-cr-subtitle></p>
            </div>
            <div class="px-6 py-4 space-y-3">
                <div class="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900" data-cr-existing></div>
                <label class="block text-sm font-medium text-gray-700" for="cr-new-name">New name</label>
                <input id="cr-new-name" type="text" class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-primary focus:border-primary" />
                <p class="text-xs text-gray-500">Edit the name above and press <strong>Rename and continue</strong> — or pick <strong>Replace existing</strong> to overwrite. Replace soft-deletes the existing item (recoverable per ISO retention).</p>
            </div>
            <div class="px-6 py-4 border-t border-gray-200 flex items-center justify-end gap-2">
                <button data-cr-cancel class="px-4 py-2 text-gray-700 bg-gray-100 rounded-md hover:bg-gray-200 text-sm">Cancel</button>
                <button data-cr-replace class="px-4 py-2 text-white bg-red-600 rounded-md hover:bg-red-700 text-sm">Replace existing</button>
                <button data-cr-rename class="px-4 py-2 text-white bg-primary rounded-md hover:bg-primary/90 text-sm">Rename and continue</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    return modal;
}

export function isNameCollisionError(error) {
    return !!(
        error &&
        error.status === 409 &&
        error.detail &&
        typeof error.detail === "object" &&
        error.detail.code === "name_collision"
    );
}

/**
 * Show the rename / replace / cancel modal seeded with the structured
 * 409 detail. Optional `opts.allowReplace=false` hides the Replace
 * button (e.g., for callers without delete permission).
 *
 * Returns Promise<{action: "rename"|"replace", new_name?: string} | null>.
 */
export function resolveCollision(error, opts = {}) {
    if (!isNameCollisionError(error)) return Promise.resolve(null);

    const detail = error.detail;
    const incoming = detail.incoming_name || "";
    const existing = detail.existing || {};
    const allowReplace = opts.allowReplace !== false;

    const modal = ensureModal();
    modal.classList.remove("hidden");
    modal.classList.add("flex");

    const titleEl = modal.querySelector("[data-cr-title]");
    const subtitleEl = modal.querySelector("[data-cr-subtitle]");
    const existingEl = modal.querySelector("[data-cr-existing]");
    const inputEl = modal.querySelector("#cr-new-name");
    const renameBtn = modal.querySelector("[data-cr-rename]");
    const replaceBtn = modal.querySelector("[data-cr-replace]");
    const cancelBtn = modal.querySelector("[data-cr-cancel]");

    titleEl.textContent = `An item named "${incoming}" already exists`;
    subtitleEl.textContent =
        opts.subtitle || "Rename, replace, or cancel.";
    existingEl.innerHTML = `
        Existing item: <strong>${escapeHtml(existing.name || "?")}</strong>
        (${escapeHtml(existing.cog_type || "?")})
        ${
            existing.modified_at
                ? ` &middot; updated ${escapeHtml(
                      new Date(existing.modified_at).toLocaleString()
                  )}`
                : ""
        }
    `;
    inputEl.value = incoming;
    inputEl.select();
    inputEl.focus();

    replaceBtn.style.display = allowReplace ? "" : "none";

    return new Promise((resolve) => {
        const close = (result) => {
            modal.classList.add("hidden");
            modal.classList.remove("flex");
            // Detach listeners — modal element is re-used.
            renameBtn.removeEventListener("click", onRename);
            replaceBtn.removeEventListener("click", onReplace);
            cancelBtn.removeEventListener("click", onCancel);
            modal.removeEventListener("click", onBackdrop);
            document.removeEventListener("keydown", onKey);
            resolve(result);
        };

        const onRename = () => {
            const v = (inputEl.value || "").trim();
            if (!v) {
                inputEl.focus();
                return;
            }
            if (v === incoming) {
                // User didn't change the name — same as cancel, otherwise
                // we'd just hit the same 409 again on retry.
                close(null);
                return;
            }
            close({ action: "rename", new_name: v });
        };
        const onReplace = () => close({ action: "replace" });
        const onCancel = () => close(null);
        const onBackdrop = (e) => {
            if (e.target === modal) close(null);
        };
        const onKey = (e) => {
            if (e.key === "Escape") close(null);
            else if (e.key === "Enter" && document.activeElement === inputEl) {
                e.preventDefault();
                onRename();
            }
        };

        renameBtn.addEventListener("click", onRename);
        replaceBtn.addEventListener("click", onReplace);
        cancelBtn.addEventListener("click", onCancel);
        modal.addEventListener("click", onBackdrop);
        document.addEventListener("keydown", onKey);
    });
}
