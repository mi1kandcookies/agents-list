// In-memory name tree, optionally mirrored to a JSON file so a live sidecar
// can resume half-finished operations after a restart.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export class NameStore {
  constructor({ file = '' } = {}) {
    this.file = file;
    this.nodes = new Map();
    if (file) {
      try {
        for (const node of JSON.parse(readFileSync(file, 'utf8'))) this.nodes.set(node.name, node);
      } catch (err) {
        if (err.code !== 'ENOENT') throw err;
      }
    }
  }

  get(name) { return this.nodes.get(name); }

  put(node) {
    this.nodes.set(node.name, node);
    this.save();
    return node;
  }

  save() {
    if (!this.file) return;
    mkdirSync(dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp`;
    writeFileSync(tmp, JSON.stringify([...this.nodes.values()], null, 2));
    renameSync(tmp, this.file);
  }

  children(name) {
    return [...this.nodes.values()].filter((n) => n.parent === name).sort((a, b) => a.name.localeCompare(b.name));
  }

  descendants(name) {
    return this.children(name).flatMap((c) => [c, ...this.descendants(c.name)]);
  }

  subtree(name) {
    const node = this.get(name);
    if (!node) return null;
    return { ...node, children: this.children(name).map((c) => this.subtree(c.name)) };
  }
}
