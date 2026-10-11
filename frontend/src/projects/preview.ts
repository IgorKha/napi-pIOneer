import type { Graph, GraphEdge } from '../api';
export type Change = 'added' | 'changed' | 'removed';
export const changeColor = { added: '#65b884', changed: '#e2af57', removed: '#e17777' };
const edgeKey = (e: GraphEdge) => JSON.stringify([e.from, e.to]);
const same = (a: unknown, b: unknown): boolean => {
  if (a === b) return true;
  if (typeof a !== 'object' || a === null || typeof b !== 'object' || b === null) return false;
  const x = a as Record<string, unknown>, y = b as Record<string, unknown>;
  const keys = Object.keys(x);
  return keys.length === Object.keys(y).length && keys.every(k => Object.hasOwn(y, k) && same(x[k], y[k]));
};
/** Display removed nodes too, without changing the proposed graph or live runtime. */
export function graphPreview(before: Graph, after: Graph) {
  const old = new Map(before.nodes.map(n => [n.id, n])), next = new Map(after.nodes.map(n => [n.id, n]));
  const nodes = new Map<string, Change>();
  for (const n of after.nodes) if (!old.has(n.id)) nodes.set(n.id, 'added'); else if (!same(old.get(n.id), n)) nodes.set(n.id, 'changed');
  for (const n of before.nodes) if (!next.has(n.id)) nodes.set(n.id, 'removed');
  const oldEdges = new Set(before.edges.map(edgeKey)), newEdges = new Set(after.edges.map(edgeKey));
  const edges = new Map<string, Change>();
  for (const key of newEdges) if (!oldEdges.has(key)) edges.set(key, 'added');
  for (const key of oldEdges) if (!newEdges.has(key)) edges.set(key, 'removed');
  return { graph: { ...after, nodes: [...after.nodes, ...before.nodes.filter(n => !next.has(n.id))],
                   edges: [...after.edges, ...before.edges.filter(e => !newEdges.has(edgeKey(e)))] }, nodes, edges };
}
export { edgeKey };
