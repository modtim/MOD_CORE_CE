/**
 * Tests for Model3DViewerModal — successful model load still works.
 *
 * Verifies (b): a resolving loader still renders the model (Suspense isn't
 * broken by the error boundary).
 */

import React from 'react';
import { render } from '@testing-library/react';

// Mock Three.js / React Three Fiber with SUCCESSFUL resolution.
jest.mock('@react-three/fiber', () => ({
  Canvas: ({ children }: any) => <div data-testid="canvas">{children}</div>,
  useLoader: () => ({ scene: { clone: () => ({}) } }),
}));
jest.mock('@react-three/drei', () => ({
  OrbitControls: () => null,
  Center: ({ children }: any) => <div data-testid="center">{children}</div>,
  useGLTF: () => ({ scene: { clone: () => ({}) } }),
  Environment: () => null,
  Grid: () => null,
}));
jest.mock('three', () => ({ DoubleSide: 2 }));
jest.mock('three/examples/jsm/loaders/OBJLoader.js', () => ({ OBJLoader: class {} }));
jest.mock('three/examples/jsm/loaders/FBXLoader.js', () => ({ FBXLoader: class {} }));
jest.mock('three/examples/jsm/loaders/PLYLoader.js', () => ({ PLYLoader: class {} }));
jest.mock('three/examples/jsm/loaders/STLLoader.js', () => ({ STLLoader: class {} }));

import { Model3DViewerModal } from '@/components/model3d-thumbnail';

describe('Model3DViewerModal — success path', () => {
  it('renders the model when the loader resolves (suspense still works)', () => {
    render(
      <Model3DViewerModal
        url="https://s3.example/tenant-2-files/abc.glb"
        ext="glb"
        onClose={() => {}}
      />
    );
    expect(document.querySelector('[data-3d-viewer]')).toBeTruthy();
    expect(document.querySelector('[data-testid="canvas"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="center"]')).toBeTruthy();
  });
});
