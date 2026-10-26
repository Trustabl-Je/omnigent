// Renders any JSON value from a tool call as labeled fields: objects as a
// key/value list, lists of records as rows with a title, detail and status
// badge, and multi-line strings with their real line breaks.

import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { absoluteTime } from "@/lib/relativeTime";
import {
  type StatusTone,
  humanizeKey,
  isRecord,
  isoTimestampMs,
  pickDetailField,
  pickStatusField,
  pickTitleField,
  statusLabel,
  statusTone,
} from "@/lib/toolValue";
import { cn } from "@/lib/utils";

// Past this depth a value is shown as compact JSON instead of nested lists.
const MAX_FIELD_DEPTH = 3;
const MAX_LIST_ROWS = 50;
const MAX_INLINE_CHIPS = 20;
const INLINE_CHIP_MAX_CHARS = 60;
const INLINE_STRING_MAX_CHARS = 300;
const EXTRA_ROW_FIELDS = 3;

export function ToolValueView({ value }: { value: unknown }) {
  return (
    <div className="min-w-0 text-sm" data-testid="tool-value-view">
      <ValueNode value={value} depth={0} />
    </div>
  );
}

function ValueNode({ value, depth }: { value: unknown; depth: number }): ReactNode {
  if (value === null || value === undefined) return <EmptyValue />;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return <span className="tabular-nums">{String(value)}</span>;
  if (typeof value === "string") return <StringValue text={value} />;
  if (Array.isArray(value)) return <ListValue items={value} depth={depth} />;
  if (isRecord(value)) {
    if (depth >= MAX_FIELD_DEPTH) return <CompactJson value={value} />;
    return <FieldList entries={Object.entries(value)} depth={depth} />;
  }
  return String(value);
}

function EmptyValue() {
  return <span className="text-muted-foreground">None</span>;
}

function FieldList({ entries, depth }: { entries: [string, unknown][]; depth: number }) {
  if (entries.length === 0) return <EmptyValue />;
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
      {entries.map(([key, value]) => (
        <FieldRow key={key} label={humanizeKey(key)} title={key}>
          <ValueNode value={value} depth={depth + 1} />
        </FieldRow>
      ))}
    </dl>
  );
}

function FieldRow({
  label,
  title,
  children,
}: {
  label: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <>
      <dt className="max-w-48 break-words text-muted-foreground" title={title}>
        {label}
      </dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </>
  );
}

function StringValue({ text }: { text: string }) {
  if (text.length === 0) return <EmptyValue />;
  if (text.includes("\n") || text.length > INLINE_STRING_MAX_CHARS) {
    return (
      <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 px-2.5 py-1.5 font-mono text-xs">
        {text}
      </pre>
    );
  }
  const ms = isoTimestampMs(text);
  if (ms !== null) {
    return (
      <time dateTime={text} title={text}>
        {absoluteTime(ms)}
      </time>
    );
  }
  if (/^https?:\/\/\S+$/.test(text)) {
    return (
      <a
        href={text}
        target="_blank"
        rel="noopener noreferrer"
        className="break-all text-primary underline-offset-2 hover:underline"
      >
        {text}
      </a>
    );
  }
  // Ids, paths and other single tokens read better in the code face.
  const isToken = !/\s/.test(text) && /[\d/._:-]/.test(text);
  return isToken ? <span className="font-mono text-[0.92em]">{text}</span> : text;
}

function ListValue({ items, depth }: { items: unknown[]; depth: number }) {
  if (items.length === 0) return <EmptyValue />;
  const isChipList =
    items.length <= MAX_INLINE_CHIPS &&
    items.every(
      (item) =>
        typeof item === "number" ||
        (typeof item === "string" && item.length > 0 && item.length <= INLINE_CHIP_MAX_CHARS),
    );
  if (isChipList) {
    return (
      <span className="flex flex-wrap gap-1">
        {items.map((item, index) => (
          // Values can repeat and the list never reorders, so the index is the key.
          // eslint-disable-next-line react/no-array-index-key
          <Badge key={index} variant="outline" className="font-normal text-xs">
            {String(item)}
          </Badge>
        ))}
      </span>
    );
  }
  if (
    depth < MAX_FIELD_DEPTH &&
    items.every(isRecord) &&
    items.some((item) => pickTitleField(item))
  ) {
    return <RecordRows records={items} />;
  }
  if (depth >= MAX_FIELD_DEPTH) return <CompactJson value={items} />;
  return (
    <FieldList
      entries={items.map((item, index): [string, unknown] => [String(index + 1), item])}
      depth={depth}
    />
  );
}

/** A list of records (todos, issues, search hits) as one row each. */
function RecordRows({ records }: { records: Record<string, unknown>[] }) {
  const shown = records.slice(0, MAX_LIST_ROWS);
  const hidden = records.length - shown.length;
  return (
    <ol className="grid gap-1.5">
      {shown.map((record, index) => (
        // Records carry no guaranteed id and the list never reorders.
        // eslint-disable-next-line react/no-array-index-key
        <RecordRow key={index} record={record} position={index + 1} />
      ))}
      {hidden > 0 && <li className="text-muted-foreground text-xs">and {hidden} more</li>}
    </ol>
  );
}

function RecordRow({ record, position }: { record: Record<string, unknown>; position: number }) {
  const title = pickTitleField(record);
  const detail = pickDetailField(record);
  const status = pickStatusField(record);
  const index = typeof record.id === "number" ? record.id : position;
  const shownKeys = new Set([title?.key, detail?.key, status?.key, "id"]);
  const extras = Object.entries(record).filter(
    ([key, value]) =>
      !shownKeys.has(key) && (typeof value === "string" || typeof value === "number"),
  );
  return (
    <li className="grid grid-cols-[1.25rem_minmax(0,1fr)_auto] gap-x-2">
      <span className="text-muted-foreground/70 text-xs tabular-nums leading-[inherit]">
        {index}
      </span>
      <span className="min-w-0 break-words">{title?.value ?? <EmptyValue />}</span>
      {status === null ? <span /> : <StatusBadge status={status.value} />}
      {detail !== null && (
        <span className="col-span-2 col-start-2 text-muted-foreground text-xs leading-snug">
          {detail.value}
        </span>
      )}
      {extras.length > 0 && (
        <span className="col-span-2 col-start-2 truncate text-muted-foreground text-xs">
          {extras.slice(0, EXTRA_ROW_FIELDS).map(([key, value], extraIndex) => (
            <span key={key}>
              {extraIndex > 0 && " · "}
              {humanizeKey(key)} <StringValue text={String(value)} />
            </span>
          ))}
          {extras.length > EXTRA_ROW_FIELDS && ` · +${extras.length - EXTRA_ROW_FIELDS} more`}
        </span>
      )}
    </li>
  );
}

const STATUS_TONE_CLASS: Record<StatusTone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  active: "border-info/40 bg-info/10 text-info",
  failure: "border-destructive/40 bg-destructive/10 text-destructive",
  neutral: "text-muted-foreground",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <Badge
      variant="outline"
      className={cn("h-[1.15rem] font-normal text-xs", STATUS_TONE_CLASS[statusTone(status)])}
    >
      {statusLabel(status)}
    </Badge>
  );
}

function CompactJson({ value }: { value: unknown }) {
  return <span className="break-all font-mono text-xs">{JSON.stringify(value)}</span>;
}
