/** Виджеты studio: каждый элемент интерфейса существует ровно здесь —
 * со своей разметкой, стилями и типизированными вариантами. Страницы и
 * панели собираются из этих виджетов и не пишут классы элементов руками
 * (это охраняет eslint-правило no-restricted-syntax). */

export { Alert } from "./Alert";
export { Button, type ButtonSize, type ButtonTone } from "./Button";
export { Chip, type ChipTone } from "./Chip";
export { Code } from "./Code";
export { Cell, DataTable, TableRow, type CellMod } from "./DataTable";
export { Dialog } from "./Dialog";
export { EmptyState } from "./EmptyState";
export { Eyebrow } from "./Eyebrow";
export { Facts, type Fact } from "./Facts";
export { Field } from "./Field";
export { IconButton, type IconButtonSize } from "./IconButton";
export { IconLink } from "./IconLink";
export { Input } from "./Input";
export { ItemRow } from "./ItemRow";
export { Detail, Form, Index, IndexHead, Page, PageBody, PageNotices, Pane, Row, Scene } from "./Layout";
export { LinkButton } from "./LinkButton";
export { List, ListAside, ListName, ListRow, type ListKind } from "./List";
export { Menu, MenuGroup, MenuItem, MenuList } from "./Menu";
export { Note, type NoteTone } from "./Note";
export { Panel, PanelHead, Section, SectionHead, SectionText } from "./Panel";
export { Search } from "./Search";
export { Segmented } from "./Segmented";
export { Select } from "./Select";
export { SidePanel } from "./SidePanel";
export { StatusDot } from "./StatusDot";
export { TextArea } from "./TextArea";
export { ToastProvider, useToast, type ToastFn, type ToastTone } from "./Toast";
export { Toolbar, ToolbarHint, ToolbarLabel, ToolbarSpacer } from "./Toolbar";
export { Topbar, TopbarGroup, TopbarHint, TopbarLink, TopbarSpacer, TopbarTitle } from "./Topbar";
export { narrowScreen, useNarrowScreen } from "./useNarrowScreen";
