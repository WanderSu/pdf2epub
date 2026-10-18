import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Node 22+ 内置了实验性 localStorage 全局(未传 --localstorage-file 时为 undefined),
// 它会盖掉 jsdom 注入的那份,导致测试里 localStorage.setItem 直接 TypeError。
// 这里显式装一个内存实现,不依赖运行环境的 node 版本。
class MemoryStorage implements Storage {
  private map = new Map<string, string>();

  get length(): number {
    return this.map.size;
  }

  clear(): void {
    this.map.clear();
  }

  getItem(key: string): string | null {
    return this.map.has(key) ? this.map.get(key)! : null;
  }

  key(index: number): string | null {
    return Array.from(this.map.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.map.delete(key);
  }

  setItem(key: string, value: string): void {
    this.map.set(key, String(value));
  }
}

function installStorage(): void {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  for (const target of [globalThis, typeof window === "undefined" ? undefined : window]) {
    if (!target) continue;
    for (const [name, value] of [["localStorage", local], ["sessionStorage", session]] as const) {
      try {
        Object.defineProperty(target, name, { value, configurable: true, writable: true });
      } catch {
        // 极少数环境里属性不可重定义,退回直接赋值
        try { (target as unknown as Record<string, unknown>)[name] = value; } catch { /* 放弃 */ }
      }
    }
  }
}

// 装载阶段先来一份,避免任何在 it 之外就读 storage 的代码拿不到;
// 每个用例前重装,保证干净且不会被别的库换掉。
installStorage();
beforeEach(() => installStorage());

// 每个用例后卸载组件,避免上一个用例的 DOM 泄漏到下一个
afterEach(() => {
  cleanup();
  globalThis.localStorage?.clear();
  globalThis.sessionStorage?.clear();
});
