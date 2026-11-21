/**
 * ConnectionLostBanner accessible name/description (2400LR-b2z).
 *
 * The role="alert" container used to have no aria-labelledby/aria-describedby,
 * so getByRole('alert', { name: 'Workers unavailable' }) could not resolve.
 */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { ConnectionLostBanner } from '@/components/connection-lost-banner';
import { AUTH_CONNECTION_LOST_EVENT } from '@/lib/api-client';

function fireLost() {
  act(() => {
    window.dispatchEvent(new CustomEvent(AUTH_CONNECTION_LOST_EVENT, { detail: { reason: 'test' } }));
  });
}

describe('ConnectionLostBanner a11y', () => {
  it('exposes the title as the alert name and the body as its description', () => {
    render(<ConnectionLostBanner />);
    expect(screen.queryByRole('alert')).toBeNull();
    fireLost();
    const alert = screen.getByRole('alert', { name: 'Workers unavailable' });
    expect(alert).toHaveAccessibleDescription(/We can't reach the MOD API/);
  });

  it('uses distinct ids when mounted twice', () => {
    render(
      <>
        <ConnectionLostBanner />
        <ConnectionLostBanner />
      </>,
    );
    fireLost();
    const alerts = screen.getAllByRole('alert', { name: 'Workers unavailable' });
    expect(alerts).toHaveLength(2);
    expect(alerts[0].getAttribute('aria-labelledby')).not.toBe(
      alerts[1].getAttribute('aria-labelledby'),
    );
  });
});
