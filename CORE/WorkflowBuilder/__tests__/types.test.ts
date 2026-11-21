/**
 * Tests for lib/types.ts — TypeScript type definitions.
 * Verifies that data structures match the API contract.
 */

import type { NodeData, PreviewAsset, WorkflowVersion } from '@/lib/types';

describe('PreviewAsset type', () => {
  it('accepts all 11 category values', () => {
    const categories: PreviewAsset['category'][] = [
      'image', 'model3d', 'video', 'audio', 'text', 'pdf',
      'splat', 'pointcloud', 'font', 'archive', 'other',
    ];
    // This is a compile-time check — if any category is invalid, TS will error
    expect(categories).toHaveLength(11);
  });

  it('structures match API response', () => {
    const asset: PreviewAsset = {
      url: 'https://minio.example.com/file?token=abc',
      file_extension: 'png',
      category: 'image',
      content_hash: 'a'.repeat(64),
      logical_name: 'output.png',
    };
    expect(asset.url).toBeTruthy();
    expect(asset.content_hash).toHaveLength(64);
  });
});

describe('NodeData type', () => {
  it('includes preview-related fields', () => {
    const data: NodeData = {
      label: 'Test Node',
      status: 'success',
      previewAssets: [{
        url: 'http://example.com/thumb.png',
        file_extension: 'png',
        category: 'image',
        content_hash: 'abc123',
        logical_name: 'out.png',
      }],
      error_message: 'some error',
      started_at: '2026-01-01T00:00:00Z',
      inputPreviewUrl: 'http://example.com/input.png',
      inputPreviewCategory: 'image',
    };
    expect(data.previewAssets).toHaveLength(1);
    expect(data.error_message).toBe('some error');
    expect(data.started_at).toBeTruthy();
  });

  it('all fields are optional except label', () => {
    const minimal: NodeData = { label: 'Minimal' };
    expect(minimal.label).toBe('Minimal');
    expect(minimal.status).toBeUndefined();
    expect(minimal.previewAssets).toBeUndefined();
    expect(minimal.error_message).toBeUndefined();
  });
});

describe('WorkflowVersion type', () => {
  it('graph_json contains nodes and edges', () => {
    const version: WorkflowVersion = {
      id: 1,
      version_number: 1,
      graph_json: {
        nodes: [],
        edges: [],
      },
      created_at: '2026-01-01T00:00:00Z',
    };
    expect(version.graph_json.nodes).toEqual([]);
    expect(version.graph_json.edges).toEqual([]);
  });
});
