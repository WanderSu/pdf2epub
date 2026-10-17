import { defineConfig } from "vitest/config";
import path from "node:path";

// 前端单测:跑 src 下的纯逻辑与组件,不碰 Tauri 的 IPC。
// 别名与 vite.config 保持一致,测试里可以 `import { ... } from "@/format"`。
export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    // jsdom 默认起独立 worker,会在系统 temp 写模块运行时;受限环境(沙盒/CI 容器)
    // 下会 EPERM。单 fork 顺序跑全套 3 秒内完成,并行收益不值这个坑。
    pool: "forks",
    poolOptions: { forks: { singleFork: true } },
  },
});
