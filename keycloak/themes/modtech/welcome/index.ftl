<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="robots" content="noindex, nofollow" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light" />
    <title>${realm.displayName!productName!'Mod Tech Labs'}</title>
    <link rel="icon" type="image/svg+xml" href="${resourcesPath}/img/favicon.svg" />
    <link rel="stylesheet" href="${resourcesPath}/css/welcome.css" />
</head>
<body>
<div class="mt-welcome">
    <header class="mt-welcome-header">
        <img class="mt-welcome-logo" src="${resourcesPath}/img/wordmark.png" alt="Mod Tech Labs" />
    </header>
    <main class="mt-welcome-card">

        <#if adminConsoleEnabled && (bootstrap || successMessage?has_content)>
            <!-- First-run admin bootstrap (server's first launch). -->
            <#if localUser>
                <h1 class="mt-welcome-title">Create the temporary admin user</h1>
                <p class="mt-welcome-lede">
                    Get started by creating a temporary administrative user. Once
                    you've signed in you should create a permanent admin and
                    delete this one.
                </p>
                <#if !successMessage?has_content>
                    <form method="post" class="mt-welcome-form" novalidate>
                        <input type="hidden" id="stateChecker" name="stateChecker" value="${stateChecker}" />
                        <div class="mt-welcome-field">
                            <label for="username">Username</label>
                            <input id="username" name="username" type="text" autocomplete="username" autofocus required />
                        </div>
                        <div class="mt-welcome-field">
                            <label for="password">Password</label>
                            <input id="password" name="password" type="password" autocomplete="new-password" required />
                        </div>
                        <div class="mt-welcome-field">
                            <label for="passwordConfirmation">Confirm password</label>
                            <input id="passwordConfirmation" name="passwordConfirmation" type="password" autocomplete="new-password" required />
                        </div>
                        <#if errorMessage?has_content>
                            <p class="mt-welcome-error">${errorMessage}</p>
                        </#if>
                        <button type="submit" class="mt-welcome-btn">Create user</button>
                    </form>
                <#else>
                    <div class="mt-welcome-success">
                        <p>${successMessage}</p>
                    </div>
                </#if>
            <#else>
                <h1 class="mt-welcome-title">Local access required</h1>
                <p class="mt-welcome-lede">
                    The temporary admin user can only be created from the
                    Keycloak server's own host.
                </p>
            </#if>
        <#else>
            <h1 class="mt-welcome-title">${realm.displayName!productName!'Mod Tech Labs'}</h1>
            <p class="mt-welcome-lede">
                Identity and access management for the Mod Tech Labs platform.
            </p>
            <nav class="mt-welcome-links">
                <#if adminConsoleEnabled>
                    <a class="mt-welcome-btn" href="${adminUrl}">Administration console</a>
                </#if>
                <#if accountUrl??>
                    <a class="mt-welcome-link" href="${accountUrl}">My account</a>
                </#if>
            </nav>
        </#if>
    </main>
    <footer class="mt-welcome-foot">
        <span>Powered by Keycloak</span>
        <span class="mt-welcome-version">${productVersion!''}</span>
    </footer>
</div>
</body>
</html>
