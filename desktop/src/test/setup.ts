import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// 每个用例后卸载组件,避免上一个用例的 DOM 泄漏到下一个
afterEach(() => {
  cleanup();
  localStorage.clear();
});
