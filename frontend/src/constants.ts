export const APP_RAIL_ITEMS = [
  { id: "overview", label: "首页", icon: "⌂" },
  { id: "sources", label: "资料", icon: "▦" },
  { id: "wiki", label: "知识", icon: "◇" },
  { id: "qa", label: "问答", icon: "?" },
] as const;

export const NAV_ITEMS = [
  { id: "overview", label: "空间首页", desc: "核心数据与待办", icon: "⌂" },
  { id: "ingest", label: "导入资料", desc: "上传、扫描、标准化", icon: "+" },
  { id: "sources", label: "资料库", desc: "查看、预览、删除", icon: "▦" },
  { id: "wiki", label: "知识页", desc: "编辑与发布状态", icon: "◇" },
  { id: "qa", label: "智能问答", desc: "引用回答与缺口", icon: "?" },
  { id: "reviews", label: "审阅队列", desc: "人工确认后发布", icon: "✓" },
] as const;

export type SectionId = (typeof NAV_ITEMS)[number]["id"];
