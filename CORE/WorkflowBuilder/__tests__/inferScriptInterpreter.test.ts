/**
 * Tests for inferScriptInterpreterFromFilename — the New Node wizard's
 * script-tab uploader uses this to auto-set the interpreter dropdown
 * when the user picks a file (so they don't have to remember to
 * manually flip "Python" / "Bash" / etc.). Also returns a sentinel
 * when the user accidentally drops a Dockerfile into the script tab
 * so the wizard can redirect them.
 */

import { inferScriptInterpreterFromFilename } from '@/components/new-node-wizard';

describe('inferScriptInterpreterFromFilename', () => {
  it('returns null on empty / null filename', () => {
    expect(inferScriptInterpreterFromFilename('')).toBeNull();
  });

  it('returns null on extensionless filename', () => {
    expect(inferScriptInterpreterFromFilename('myfile')).toBeNull();
  });

  it('returns null on unknown extension', () => {
    expect(inferScriptInterpreterFromFilename('foo.unknownext')).toBeNull();
  });

  describe('common code extensions', () => {
    const cases: Array<[string, string]> = [
      ['main.py', 'python3'],
      ['run.py', 'python3'],
      ['build.sh', 'bash'],
      ['install.bash', 'bash'],
      ['app.js', 'node'],
      ['app.mjs', 'node'],
      ['app.ts', 'typescript'],
      ['app.rb', 'ruby'],
      ['index.php', 'php'],
      ['script.pl', 'perl'],
      ['plugin.lua', 'lua'],
      ['main.go', 'go'],
      ['main.rs', 'rust'],
      ['main.c', 'c'],
      ['main.cpp', 'cpp'],
      ['main.cc', 'cpp'],
      ['main.cxx', 'cpp'],
      ['Main.java', 'java'],
      ['analyze.r', 'r'],
      ['data.json', 'json'],
      ['config.yaml', 'yaml'],
      ['config.yml', 'yaml'],
      ['Cargo.toml', 'toml'],
      ['data.xml', 'xml'],
    ];
    it.each(cases)('%s → %s', (filename, expected) => {
      expect(inferScriptInterpreterFromFilename(filename)).toBe(expected);
    });
  });

  describe('plain-text files', () => {
    it('treats requirements.txt as text', () => {
      expect(inferScriptInterpreterFromFilename('requirements.txt')).toBe('text');
    });
    it('treats Pipfile as text', () => {
      expect(inferScriptInterpreterFromFilename('Pipfile')).toBe('text');
    });
    it('treats pyproject.toml as text (Python project metadata)', () => {
      // pyproject.toml is more useful as the canonical project-meta
      // file than as a generic .toml — caller wants the "text" handling.
      expect(inferScriptInterpreterFromFilename('pyproject.toml')).toBe('text');
    });
    it('treats arbitrary .txt as text', () => {
      expect(inferScriptInterpreterFromFilename('notes.txt')).toBe('text');
    });
  });

  describe('Dockerfile redirect sentinel', () => {
    it('returns DOCKERFILE_REDIRECT for "Dockerfile"', () => {
      expect(inferScriptInterpreterFromFilename('Dockerfile')).toBe('DOCKERFILE_REDIRECT');
    });
    it('returns DOCKERFILE_REDIRECT for case variants', () => {
      expect(inferScriptInterpreterFromFilename('dockerfile')).toBe('DOCKERFILE_REDIRECT');
      expect(inferScriptInterpreterFromFilename('DOCKERFILE')).toBe('DOCKERFILE_REDIRECT');
    });
    it('returns DOCKERFILE_REDIRECT for *.dockerfile', () => {
      expect(inferScriptInterpreterFromFilename('app.dockerfile')).toBe('DOCKERFILE_REDIRECT');
    });
  });

  describe('webkitRelativePath inputs (folder uploads)', () => {
    it('strips folder prefix when extracting basename', () => {
      expect(inferScriptInterpreterFromFilename('myproject/main.py')).toBe('python3');
    });
    it('strips deep folder prefix', () => {
      expect(inferScriptInterpreterFromFilename('a/b/c/requirements.txt')).toBe('text');
    });
    it('strips folder prefix for Dockerfile redirect too', () => {
      expect(inferScriptInterpreterFromFilename('myproject/Dockerfile')).toBe('DOCKERFILE_REDIRECT');
    });
  });
});
