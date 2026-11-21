"""
Email Template Renderer

Renders HTML email templates with variable substitution using Jinja2.
"""

from typing import Dict, Any
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from datetime import datetime


class EmailRenderer:
    """Renders email templates with context data."""

    def __init__(self, templates_dir: str = None):
        """
        Initialize the email renderer.

        Args:
            templates_dir: Path to email templates directory.
                          Defaults to /workspace/CORE/API/email_templates
        """
        if templates_dir is None:
            templates_dir = str(Path(__file__).parent.parent / "email_templates")

        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(['html', 'xml'])
        )

        # Add custom filters
        self.env.filters['pluralize'] = self._pluralize

    def _pluralize(self, count: int, singular: str = '', plural: str = 's') -> str:
        """Return singular or plural suffix based on count."""
        return singular if count == 1 else plural

    def render_expiration_warning(
        self,
        user_name: str,
        user_email: str,
        folder_name: str,
        permission_level: str,
        expiration_date: str,
        days_until_expiration: int,
        granted_by: str,
        granted_by_email: str,
        is_external_user: bool = False,
        capabilities: Dict[str, bool] = None,
        dashboard_url: str = "https://your-app.com/dashboard",
        support_url: str = "https://your-app.com/support"
    ) -> str:
        """
        Render the permission expiration warning email for the user.

        Args:
            user_name: Name of the user receiving the warning
            user_email: Email of the user
            folder_name: Name of the folder they have access to
            permission_level: Their permission level (viewer/editor/owner)
            expiration_date: Formatted expiration date string
            days_until_expiration: Number of days until expiration
            granted_by: Name of the person who granted the permission
            granted_by_email: Email of the granter
            is_external_user: Whether this is an external user
            capabilities: Dict of permission capabilities
            dashboard_url: URL to the dashboard
            support_url: URL to support

        Returns:
            Rendered HTML string
        """
        # Determine alert styling based on days remaining
        if days_until_expiration == 1:
            alert_class = "urgent"
            countdown_class = "urgent"
            alert_title = "⚠️ Final Warning"
        elif days_until_expiration <= 3:
            alert_class = "urgent"
            countdown_class = "warning"
            alert_title = "⚠️ Urgent: Access Expiring Soon"
        else:
            alert_class = "warning"
            countdown_class = ""
            alert_title = "⏰ Reminder: Access Expiring"

        template = self.env.get_template('permission_expiration_warning.html')

        context = {
            'user_name': user_name,
            'user_email': user_email,
            'folder_name': folder_name,
            'permission_level': permission_level.title(),
            'expiration_date': expiration_date,
            'days_until_expiration': days_until_expiration,
            'days_plural': '' if days_until_expiration == 1 else 's',
            'granted_by': granted_by,
            'granted_by_email': granted_by_email,
            'is_external_user': is_external_user,
            'alert_class': alert_class,
            'countdown_class': countdown_class,
            'alert_title': alert_title,
            'dashboard_url': dashboard_url,
            'support_url': support_url,
            'year': datetime.now().year,
            'has_capabilities': capabilities is not None,
            **(capabilities or {})
        }

        return template.render(**context)

    def render_granter_notice(
        self,
        granter_name: str,
        granter_email: str,
        user_name: str,
        user_email: str,
        folder_name: str,
        permission_level: str,
        expiration_date: str,
        granted_date: str,
        days_until_expiration: int,
        invitation_message: str = None,
        extend_permission_url: str = None,
        manage_permissions_url: str = None,
        dashboard_url: str = "https://your-app.com/dashboard",
        support_url: str = "https://your-app.com/support"
    ) -> str:
        """
        Render the granter notification email.

        Args:
            granter_name: Name of the person who granted the permission
            granter_email: Email of the granter
            user_name: Name of the user who has the permission
            user_email: Email of the user
            folder_name: Name of the shared folder
            permission_level: Permission level granted
            expiration_date: Formatted expiration date
            granted_date: Formatted date when permission was granted
            days_until_expiration: Days remaining
            invitation_message: Original invitation message (optional)
            extend_permission_url: URL to extend the permission
            manage_permissions_url: URL to manage permissions
            dashboard_url: URL to dashboard
            support_url: URL to support

        Returns:
            Rendered HTML string
        """
        template = self.env.get_template('permission_expiration_granter_notice.html')

        context = {
            'granter_name': granter_name,
            'granter_email': granter_email,
            'user_name': user_name,
            'user_email': user_email,
            'folder_name': folder_name,
            'permission_level': permission_level.title(),
            'expiration_date': expiration_date,
            'granted_date': granted_date,
            'days_until_expiration': days_until_expiration,
            'days_plural': '' if days_until_expiration == 1 else 's',
            'days_until_expiration_eq_1': days_until_expiration == 1,
            'invitation_message': invitation_message,
            'extend_permission_url': extend_permission_url or f"{dashboard_url}/permissions/extend",
            'manage_permissions_url': manage_permissions_url or f"{dashboard_url}/permissions",
            'dashboard_url': dashboard_url,
            'support_url': support_url,
            'year': datetime.now().year
        }

        return template.render(**context)

    def render_admin_summary(
        self,
        report_date: str,
        warnings_7d: int,
        warnings_3d: int,
        warnings_1d: int,
        revoked_count: int,
        warning_errors: int,
        revoke_errors: int,
        expiring_7d_list: list = None,
        expiring_3d_list: list = None,
        expiring_1d_list: list = None,
        revoked_list: list = None,
        logs_url: str = None,
        dashboard_url: str = "https://your-app.com/dashboard",
        settings_url: str = "https://your-app.com/settings"
    ) -> str:
        """
        Render the admin summary email.

        Args:
            report_date: Formatted date of the report
            warnings_7d: Count of 7-day warnings sent
            warnings_3d: Count of 3-day warnings sent
            warnings_1d: Count of 1-day warnings sent
            revoked_count: Count of permissions revoked
            warning_errors: Count of warning email failures
            revoke_errors: Count of revocation failures
            expiring_7d_list: List of permissions expiring in 7 days
            expiring_3d_list: List of permissions expiring in 3 days
            expiring_1d_list: List of permissions expiring in 1 day
            revoked_list: List of revoked permissions
            logs_url: URL to view logs
            dashboard_url: URL to dashboard
            settings_url: URL to settings

        Returns:
            Rendered HTML string
        """
        template = self.env.get_template('permission_expiration_admin_summary.html')

        from datetime import timedelta
        next_report = datetime.now() + timedelta(days=1)

        context = {
            'report_date': report_date,
            'warnings_7d': warnings_7d,
            'warnings_3d': warnings_3d,
            'warnings_1d': warnings_1d,
            'revoked_count': revoked_count,
            'warning_errors': warning_errors,
            'revoke_errors': revoke_errors,
            'has_errors': (warning_errors + revoke_errors) > 0,
            'expiring_7d_list': expiring_7d_list or [],
            'expiring_3d_list': expiring_3d_list or [],
            'expiring_1d_list': expiring_1d_list or [],
            'revoked_list': revoked_list or [],
            'expiring_7d_count': len(expiring_7d_list or []),
            'expiring_3d_count': len(expiring_3d_list or []),
            'expiring_1d_count': len(expiring_1d_list or []),
            'has_activity': any([warnings_7d, warnings_3d, warnings_1d, revoked_count]),
            'logs_url': logs_url or f"{dashboard_url}/logs",
            'dashboard_url': dashboard_url,
            'settings_url': settings_url,
            'next_report_date': next_report.strftime('%Y-%m-%d at 9:00 AM UTC'),
            'year': datetime.now().year
        }

        return template.render(**context)

    def render_invoice_generated(
        self,
        admin_name: str,
        invoice_number: str,
        billing_period: str,
        invoice_date: str,
        due_date: str,
        amount_due: str,
        credits_used: int,
        payment_method: str = None,
        auto_charge: bool = False,
        usage_items: list = None,
        billing_url: str = "https://your-app.com/billing",
        platform_name: str = "Your Platform",
        support_email: str = None
    ) -> str:
        """
        Render the invoice generated email notification.

        Args:
            admin_name: Name of the tenant admin
            invoice_number: Unique invoice identifier
            billing_period: Billing period (e.g., "November 2025")
            invoice_date: Date invoice was generated
            due_date: Payment due date
            amount_due: Amount due formatted as string (e.g., "150.00")
            credits_used: Number of credits consumed
            payment_method: Payment method description (e.g., "Visa ending in 4242")
            auto_charge: Whether payment will be auto-charged
            usage_items: List of dicts with 'description' and 'credits' keys
            billing_url: URL to billing dashboard

        Returns:
            Rendered HTML string
        """
        template = self.env.get_template('invoice_generated.html')

        context = {
            'admin_name': admin_name,
            'invoice_number': invoice_number,
            'billing_period': billing_period,
            'invoice_date': invoice_date,
            'due_date': due_date,
            'amount_due': amount_due,
            'credits_used': credits_used,
            'payment_method': payment_method,
            'auto_charge': auto_charge,
            'usage_items': usage_items or [],
            'billing_url': billing_url,
            'year': datetime.now().year,
            'platform_name': platform_name,
            'support_email': support_email
        }

        return template.render(**context)

    def render_payment_success(
        self,
        admin_name: str,
        invoice_number: str,
        billing_period: str,
        payment_date: str,
        amount_paid: str,
        payment_method: str = None,
        transaction_id: str = None,
        billing_url: str = "https://your-app.com/billing",
        platform_name: str = "Your Platform",
        support_email: str = None
    ) -> str:
        """
        Render the payment success email notification.

        Args:
            admin_name: Name of the tenant admin
            invoice_number: Unique invoice identifier
            billing_period: Billing period (e.g., "November 2025")
            payment_date: Date payment was processed
            amount_paid: Amount paid formatted as string (e.g., "150.00")
            payment_method: Payment method description
            transaction_id: Stripe payment intent ID or similar
            billing_url: URL to billing dashboard

        Returns:
            Rendered HTML string
        """
        template = self.env.get_template('payment_success.html')

        context = {
            'admin_name': admin_name,
            'invoice_number': invoice_number,
            'billing_period': billing_period,
            'payment_date': payment_date,
            'amount_paid': amount_paid,
            'payment_method': payment_method,
            'transaction_id': transaction_id,
            'billing_url': billing_url,
            'year': datetime.now().year,
            'platform_name': platform_name,
            'support_email': support_email
        }

        return template.render(**context)

    def render_payment_failed(
        self,
        admin_name: str,
        invoice_number: str,
        billing_period: str,
        due_date: str,
        amount_due: str,
        failure_reason: str = None,
        payment_method: str = None,
        next_retry_date: str = None,
        billing_url: str = "https://your-app.com/billing",
        platform_name: str = "Your Platform",
        support_email: str = None
    ) -> str:
        """
        Render the payment failed email notification.

        Args:
            admin_name: Name of the tenant admin
            invoice_number: Unique invoice identifier
            billing_period: Billing period (e.g., "November 2025")
            due_date: Original payment due date
            amount_due: Amount still owed formatted as string (e.g., "150.00")
            failure_reason: Reason for payment failure
            payment_method: Payment method description
            next_retry_date: When the next automatic retry will occur
            billing_url: URL to billing dashboard

        Returns:
            Rendered HTML string
        """
        template = self.env.get_template('payment_failed.html')

        context = {
            'admin_name': admin_name,
            'invoice_number': invoice_number,
            'billing_period': billing_period,
            'due_date': due_date,
            'amount_due': amount_due,
            'failure_reason': failure_reason,
            'payment_method': payment_method,
            'next_retry_date': next_retry_date,
            'billing_url': billing_url,
            'year': datetime.now().year,
            'platform_name': platform_name,
            'support_email': support_email
        }

        return template.render(**context)
