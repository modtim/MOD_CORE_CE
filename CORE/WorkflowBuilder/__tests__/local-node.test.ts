import { isLocalNodeId } from '../lib/local-node';

const localNodes = [{ id: 999 }, { id: 998 }, { id: 997 }];

describe('isLocalNodeId', () => {
  test.each([997, 998, 999])('sentinel id %i is local', (id) => {
    expect(isLocalNodeId(id, localNodes)).toBe(true);
  });
  test.each([1, 899, 900, 996, 1000, 1154])('server id %i is not local', (id) => {
    expect(isLocalNodeId(id, localNodes)).toBe(false);
  });
  test('null/undefined are not local', () => {
    expect(isLocalNodeId(null, localNodes)).toBe(false);
    expect(isLocalNodeId(undefined, localNodes)).toBe(false);
  });
});
