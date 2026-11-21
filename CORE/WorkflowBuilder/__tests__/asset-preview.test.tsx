/**
 * Tests for AssetPreview component — renders previews for 11+ file types.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { AssetPreview } from '@/components/nodes/asset-preview';

// Mock Three.js / React Three Fiber (heavy WebGL deps not available in jsdom)
jest.mock('@react-three/fiber', () => ({
  Canvas: ({ children }: any) => <div data-testid="canvas">{children}</div>,
  useLoader: () => ({}),
}));
jest.mock('@react-three/drei', () => ({
  OrbitControls: () => null,
  Center: ({ children }: any) => <div>{children}</div>,
  useGLTF: () => ({ scene: { clone: () => ({}) } }),
  Environment: () => null,
}));
jest.mock('three', () => ({
  DoubleSide: 2,
}));
jest.mock('three/examples/jsm/loaders/OBJLoader.js', () => ({ OBJLoader: class {} }));
jest.mock('three/examples/jsm/loaders/FBXLoader.js', () => ({ FBXLoader: class {} }));
jest.mock('three/examples/jsm/loaders/PLYLoader.js', () => ({ PLYLoader: class {} }));
jest.mock('three/examples/jsm/loaders/STLLoader.js', () => ({ STLLoader: class {} }));

describe('AssetPreview', () => {
  it('returns null for empty assets', () => {
    const { container } = render(<AssetPreview assets={[]} />);
    expect(container.innerHTML).toBe('');
  });

  it('renders image preview', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/test.png',
        file_extension: 'png',
        category: 'image',
        content_hash: 'abc',
        logical_name: 'output.png',
      }]} />
    );
    const img = document.querySelector('img');
    expect(img).toBeTruthy();
    expect(img?.src).toContain('test.png');
  });

  it('renders video preview', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/test.mp4',
        file_extension: 'mp4',
        category: 'video',
        content_hash: 'def',
        logical_name: 'output.mp4',
      }]} />
    );
    const video = document.querySelector('video');
    expect(video).toBeTruthy();
    expect(video?.src).toContain('test.mp4');
  });

  it('renders audio preview with controls', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/test.mp3',
        file_extension: 'mp3',
        category: 'audio',
        content_hash: 'ghi',
        logical_name: 'song.mp3',
      }]} />
    );
    const audio = document.querySelector('audio');
    expect(audio).toBeTruthy();
    expect(audio?.hasAttribute('controls')).toBe(true);
  });

  it('renders text preview', () => {
    // TextPreview fetches content — mock fetch
    global.fetch = jest.fn().mockResolvedValue({
      text: () => Promise.resolve('hello world'),
    }) as any;

    render(
      <AssetPreview assets={[{
        url: 'http://example.com/test.txt',
        file_extension: 'txt',
        category: 'text',
        content_hash: 'jkl',
        logical_name: 'readme.txt',
      }]} />
    );
    const pre = document.querySelector('pre');
    expect(pre).toBeTruthy();
  });

  it('renders PDF preview as iframe', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/doc.pdf',
        file_extension: 'pdf',
        category: 'pdf',
        content_hash: 'mno',
        logical_name: 'document.pdf',
      }]} />
    );
    const iframe = document.querySelector('iframe');
    expect(iframe).toBeTruthy();
    expect(iframe?.src).toContain('doc.pdf');
  });

  it('renders archive preview with icon', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/data.zip',
        file_extension: 'zip',
        category: 'archive',
        content_hash: 'pqr',
        logical_name: 'data.zip',
      }]} />
    );
    // Archive shows the filename
    expect(screen.getAllByText('data.zip').length).toBeGreaterThan(0);
  });

  it('renders splat preview with label', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/scene.splat',
        file_extension: 'splat',
        category: 'splat',
        content_hash: 'stu',
        logical_name: 'scene.splat',
      }]} />
    );
    expect(screen.getAllByText('scene.splat').length).toBeGreaterThan(0);
  });

  it('renders font preview with sample text', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/font.ttf',
        file_extension: 'ttf',
        category: 'font',
        content_hash: 'vwx',
        logical_name: 'myfont.ttf',
      }]} />
    );
    expect(screen.getByText('AaBb 123')).toBeTruthy();
  });

  it('shows navigation for multiple assets', () => {
    render(
      <AssetPreview assets={[
        { url: 'a.png', file_extension: 'png', category: 'image', content_hash: '1', logical_name: 'a.png' },
        { url: 'b.png', file_extension: 'png', category: 'image', content_hash: '2', logical_name: 'b.png' },
      ]} />
    );
    expect(screen.getByText('1/2')).toBeTruthy();
  });

  it('renders other/unknown type with filename', () => {
    render(
      <AssetPreview assets={[{
        url: 'http://example.com/data.xyz',
        file_extension: 'xyz',
        category: 'other',
        content_hash: 'yz',
        logical_name: 'data.xyz',
      }]} />
    );
    expect(screen.getAllByText('data.xyz').length).toBeGreaterThan(0);
  });
});
