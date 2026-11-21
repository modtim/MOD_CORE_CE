/**
 * wizard-rebuild §10a / §10b — DOM smoke tests for BuildPlanPanel +
 * RepoDocsPanel. Confirms:
 *
 *  - Install steps render in order
 *  - Multi-port plans render input + output ports
 *  - Applied-deltas history surfaces when present
 *  - Pinned-digest badge surfaces when from_digest set
 *  - Provenance ChunkRef deep links render
 *  - RepoDocsPanel: returns null on empty chunks, groups by intent
 *    when expanded
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { BuildPlanPanel } from '@/components/build-plan-panel';
import { RepoDocsPanel } from '@/components/repo-docs-panel';


describe('BuildPlanPanel (§10a)', () => {
  it('renders install steps in the order they appear in the plan', () => {
    render(
      <BuildPlanPanel
        plan={{
          mode: 'git',
          language: 'python',
          install_steps: [
            'pip install torch',
            'pip install transformers',
            'pip install accelerate',
          ],
        }}
      />,
    );
    const items = screen.getAllByRole('listitem');
    const texts = items.map((li) => li.textContent || '');
    expect(texts[0]).toMatch(/pip install torch/);
    expect(texts[1]).toMatch(/pip install transformers/);
    expect(texts[2]).toMatch(/pip install accelerate/);
  });

  it('renders multi-port input + output ports', () => {
    render(
      <BuildPlanPanel
        plan={{
          mode: 'git',
          language: 'python',
          input_ports: [
            { name: 'image_in', type: 'image' },
            { name: 'mask_in', type: 'image' },
          ],
          output_ports: [
            { name: 'image_out', type: 'image' },
            { name: 'metadata', type: 'json' },
          ],
        }}
      />,
    );
    expect(screen.getByText(/image_in: image/)).toBeInTheDocument();
    expect(screen.getByText(/mask_in: image/)).toBeInTheDocument();
    expect(screen.getByText(/image_out: image/)).toBeInTheDocument();
    expect(screen.getByText(/metadata: json/)).toBeInTheDocument();
  });

  it('renders applied-deltas history when present', () => {
    render(
      <BuildPlanPanel
        plan={{
          mode: 'dockerfile',
          language: 'python',
          applied_deltas: [
            {
              field: 'install_steps',
              value: ['fixed'],
              reason: 'LLM patched ModuleNotFoundError: numpy',
            },
            {
              field: 'system_deps',
              value: ['ffmpeg'],
              reason: 'libavcodec missing from base image',
            },
          ],
        }}
      />,
    );
    expect(
      screen.getByText(/LLM patched ModuleNotFoundError: numpy/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/libavcodec missing from base image/),
    ).toBeInTheDocument();
  });

  it('renders pinned-digest badge when from_digest is set', () => {
    render(
      <BuildPlanPanel
        plan={{
          mode: 'git',
          language: 'python',
          from_image_ref: 'nvidia/cuda:12.4.0-runtime-ubuntu22.04',
          from_digest:
            'sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
        }}
      />,
    );
    expect(screen.getByTitle(/Pinned digest/)).toBeInTheDocument();
    expect(screen.getByText(/pinned/)).toBeInTheDocument();
  });

  it('renders provenance ChunkRef deep links when extracted_from set', () => {
    render(
      <BuildPlanPanel
        plan={{
          mode: 'git',
          language: 'python',
          install_steps: ['pip install foo'],
          extracted_from: [
            {
              source_file: 'README.md',
              start_line: 12,
              end_line: 18,
              url: 'https://github.com/example/repo/blob/main/README.md#L12-L18',
            },
          ],
        }}
      />,
    );
    const link = screen.getByRole('link', { name: /README.md:12-18/ });
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/example/repo/blob/main/README.md#L12-L18',
    );
  });

  it('omits provenance section when extracted_from is empty', () => {
    render(
      <BuildPlanPanel
        plan={{
          mode: 'git',
          language: 'python',
          install_steps: ['pip install foo'],
        }}
      />,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});


describe('RepoDocsPanel (§10b)', () => {
  it('returns null when chunks is empty', () => {
    const { container } = render(<RepoDocsPanel chunks={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders header with chunk count when chunks present', () => {
    render(
      <RepoDocsPanel
        chunks={[
          {
            source_file: 'README.md',
            start_line: 5,
            end_line: 10,
            intents: ['install'],
          },
          {
            source_file: 'setup.py',
            start_line: 1,
            end_line: 20,
            intents: ['deps'],
          },
        ]}
      />,
    );
    expect(screen.getByText(/Reference docs · 2 chunks/)).toBeInTheDocument();
  });

  it('groups chunks by intent when expanded', () => {
    render(
      <RepoDocsPanel
        defaultOpen
        chunks={[
          {
            source_file: 'README.md',
            start_line: 5,
            end_line: 10,
            intents: ['install'],
            url: 'https://github.com/x/y/blob/main/README.md#L5-L10',
          },
          {
            source_file: 'README.md',
            start_line: 30,
            end_line: 32,
            intents: ['entrypoint'],
          },
          {
            source_file: 'setup.py',
            start_line: 1,
            end_line: 20,
            intents: ['deps'],
          },
        ]}
      />,
    );
    // Intents appear twice — once as the group header, once as the
    // per-chunk badge inside the group. We just want to confirm each
    // intent rendered somewhere; getAllByText is the right matcher.
    expect(screen.getAllByText('install').length).toBeGreaterThan(0);
    expect(screen.getAllByText('entrypoint').length).toBeGreaterThan(0);
    expect(screen.getAllByText('deps').length).toBeGreaterThan(0);
    expect(screen.getByText(/README.md:5-10/)).toBeInTheDocument();
    expect(screen.getByText(/setup.py:1-20/)).toBeInTheDocument();
  });

  it('renders View on GitHub link only when chunk has url', () => {
    render(
      <RepoDocsPanel
        defaultOpen
        chunks={[
          {
            source_file: 'README.md',
            start_line: 5,
            end_line: 10,
            intents: ['install'],
            url: 'https://github.com/x/y/blob/main/README.md#L5-L10',
          },
          {
            source_file: 'setup.py',
            start_line: 1,
            end_line: 20,
            intents: ['deps'],
          },
        ]}
      />,
    );
    const links = screen.getAllByRole('link', { name: /View on GitHub/ });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute(
      'href',
      'https://github.com/x/y/blob/main/README.md#L5-L10',
    );
  });

  it('toggles between collapsed and expanded states', () => {
    render(
      <RepoDocsPanel
        chunks={[
          {
            source_file: 'README.md',
            start_line: 5,
            end_line: 10,
            intents: ['install'],
          },
        ]}
      />,
    );
    // Collapsed by default — install group not visible.
    expect(screen.queryByText('install')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/Reference docs · 1 chunk/));
    // After expanding the panel renders the intent twice (header +
    // chunk badge); just confirm at least one is visible.
    expect(screen.getAllByText('install').length).toBeGreaterThan(0);
  });
});
