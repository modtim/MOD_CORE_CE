<#macro emailLayout>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html lang="${locale.language!'en'}" dir="${(ltr)?then('ltr','rtl')}" xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light only" />
    <title>${(realm.displayName)!'Mod Tech Labs'}</title>
    <style type="text/css">
        /* Email clients are picky: keep selectors flat, use inline-friendly
           properties, avoid web fonts (most clients block them). Brand
           consistency comes from color + typographic scale + clear hierarchy. */
        body, table, td, p { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; }
        body { margin: 0; padding: 0; background-color: #f8fafc; color: #0f172a; -webkit-font-smoothing: antialiased; }
        a { color: #0333D0; text-decoration: underline; }
        a:hover { color: #022aa5; }
        .mt-wrap { width: 100%; background-color: #f8fafc; padding: 32px 16px; }
        .mt-card { max-width: 560px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06); overflow: hidden; }
        .mt-header { padding: 24px 32px; background-color: #0333D0; color: #ffffff; }
        .mt-brand { color: #ffffff; font-size: 22px; font-weight: 800; letter-spacing: -0.5px; margin: 0; }
        .mt-brand-light { font-weight: 500; color: rgba(255, 255, 255, 0.85); }
        .mt-body { padding: 24px 32px; color: #334155; font-size: 15px; line-height: 1.55; }
        .mt-body p { margin: 0 0 14px 0; }
        .mt-body a.mt-btn,
        .mt-btn { display: inline-block; background-color: #0333D0; color: #ffffff !important; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 15px; line-height: 1; }
        .mt-btn:hover { background-color: #022aa5; }
        .mt-divider { height: 1px; background: #e2e8f0; margin: 24px 0; }
        .mt-footer { padding: 20px 32px 24px 32px; border-top: 1px solid #f1f5f9; color: #64748b; font-size: 12px; line-height: 1.5; }
        .mt-footer a { color: #64748b; text-decoration: underline; }
        @media (max-width: 480px) {
            .mt-wrap { padding: 16px 8px; }
            .mt-card { border-radius: 8px; }
            .mt-header, .mt-body, .mt-footer { padding-left: 20px; padding-right: 20px; }
        }
    </style>
</head>
<body>
<div class="mt-wrap">
    <div class="mt-card">
        <div class="mt-header">
            <p class="mt-brand">MOD <span class="mt-brand-light">Tech Labs</span></p>
        </div>
        <div class="mt-body">
            <#nested>
        </div>
        <div class="mt-footer">
            <p style="margin:0 0 6px 0;">This email was sent by ${(realm.displayName)!'Mod Tech Labs'}.</p>
            <p style="margin:0;">If you weren't expecting it you can safely ignore this message.</p>
        </div>
    </div>
</div>
</body>
</html>
</#macro>
