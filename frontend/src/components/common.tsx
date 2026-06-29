import type { ReactNode } from "react";

export function Panel({ title, badge, children }: { title: string; badge: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-title">
        <h2>{title}</h2>
        <span className="pill">{badge}</span>
      </div>
      {children}
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label>{label}{children}</label>;
}

export function List<T extends { id?: string; path?: string }>({
  rows,
  empty,
  render,
}: {
  rows: T[];
  empty: string;
  render: (row: T) => ReactNode;
}) {
  return (
    <div className="list">
      {rows.length ? rows.map((row, index) => <div key={row.id || row.path || index}>{render(row)}</div>) : <div className="item"><span>{empty}</span></div>}
    </div>
  );
}

export function Item({ title, meta, children }: { title: string; meta: ReactNode[]; children?: ReactNode }) {
  return (
    <div className="item">
      <div className="item-title">{title}</div>
      <div className="meta">{meta.filter(Boolean).map((value, index) => <span className="pill" key={`${String(value)}-${index}`}>{value}</span>)}</div>
      {children && <div className="row-actions">{children}</div>}
    </div>
  );
}

export function Detail({ label, value, pre = false }: { label: string; value: ReactNode; pre?: boolean }) {
  const displayValue = value === undefined || value === null || value === "" ? "-" : value;
  return (
    <div className="detail-item">
      <span>{label}</span>
      {pre ? <pre>{displayValue}</pre> : <strong>{displayValue}</strong>}
    </div>
  );
}
