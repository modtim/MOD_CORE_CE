/**
 * Tests for Model3DViewerModal — a missing/failed thumbnail or model fetch
 * (e.g. GET /v1/assets/{id}/thumbnail -> 404, or a presigned model URL that
 * fails to load) must render the placeholder state, NOT throw out of the
 * r3f render loop (which Next.js turns into "Application error: a client-side
 * exception has occurred").
 *
 * This test verifies (a): a rejecting loader renders the viewer without
 * throwing (the error boundary catches the error).
 *
 * See model3d-thumbnail-success.test.tsx for test (b).
 */

import React from 'react';
import { render } from '@testing-library/react';

// Mock Three.js / React Three Fiber (heavy WebGL deps not available in jsdom).
// useGLTF throws during render → error boundary catches it and renders
// the fallback message instead of crashing.
jest.mock('@react-three/fiber', () => ({
  Canvas: ({ children }: any) => <div data-testid="canvas">{children}</div>,
  useLoader: () => {
    throw new Error('Could not load ...: Failed to fetch');
  },
}));
jest.mock('@react-three/drei', () => ({
  OrbitControls: () => null,
  Center: ({ children }: any) => <div data-testid="center">{children}</div>,
  useGLTF: () => {
    throw new Error('Could not load ...: Failed to fetch');
  },
  Environment: () => null,
  Grid: () => null,
}));
jest.mock('three', () => ({ DoubleSide: 2 }));
jest.mock('three/examples/jsm/loaders/OBJLoader.js', () => ({ OBJLoader: class {} }));
jest.mock('three/examples/jsm/loaders/FBXLoader.js', () => ({ FBXLoader: class {} }));
jest.mock('three/examples/jsm/loaders/PLYLoader.js', () => ({ PLYLoader: class {} }));
jest.mock('three/examples/jsm/loaders/STLLoader.js', () => ({ STLLoader: class {} }));

import { Model3DViewerModal } from '@/components/model3d-thumbnail';

describe('Model3DViewerModal — error boundary', () => {
  it('renders the placeholder without throwing when the model fetch 404s', () => {
    render(
      <Model3DViewerModal
        url="https://s3.example/tenant-2-files/abc.glb"
        ext="glb"
        onClose={() => {}}
      />
    );
    // The viewer opened and its canvas/placeholder state is present — no
    // client-side exception escaped the render.  The component uses
    // createPortal so content lands in document.body, not the render container.
    expect(document.querySelector('[data-3d-viewer]')).toBeTruthy();
  });
});
