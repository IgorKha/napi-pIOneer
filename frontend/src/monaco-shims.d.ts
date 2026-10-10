// Monaco 0.57 кладёт типы рядом с ESM-файлами не для всех путей — объявляем нужные.
declare module "monaco-editor/editor/editor.api.js" {
  export * from "monaco-editor";
}
declare module "monaco-editor/languages/definitions/python/register.js";
declare module "monaco-editor/editor/editor.worker.start.js?worker" {
  const WorkerCtor: { new (): Worker };
  export default WorkerCtor;
}
declare module "monaco-editor/features/register.all.js";
