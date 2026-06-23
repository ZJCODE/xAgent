import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { Search } from "lucide-react";
import { classNames } from "../lib/format";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

export function Button({ className, variant = "secondary", ...props }: ButtonProps) {
  return <button className={classNames("ui-button", `ui-button-${variant}`, className)} {...props} />;
}

export function IconButton({ className, ...props }: ButtonProps) {
  return <Button className={classNames("ui-icon-button", className)} variant={props.variant || "ghost"} {...props} />;
}

export function StatusBadge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "info" | "danger" | "muted";
  className?: string;
}) {
  return <span className={classNames("status-badge", `status-badge-${tone}`, className)}>{children}</span>;
}

export function PageShell({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={classNames("page-shell", className)}>{children}</div>;
}

export function PageToolbar({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="page-toolbar">
      <div className="page-title-block">
        <h2>{title}</h2>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-toolbar-actions">{actions}</div> : null}
    </section>
  );
}

export function Panel({
  children,
  className,
  compact = false,
}: {
  children: ReactNode;
  className?: string;
  compact?: boolean;
}) {
  return <section className={classNames("ui-panel", compact && "ui-panel-compact", className)}>{children}</section>;
}

export function PanelHeader({
  title,
  meta,
  actions,
}: {
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="panel-header">
      <div className="panel-header-copy">
        <h3>{title}</h3>
        {meta ? <span>{meta}</span> : null}
      </div>
      {actions ? <div className="panel-header-actions">{actions}</div> : null}
    </div>
  );
}

export function SearchField({
  value,
  onChange,
  onSubmit,
  placeholder,
}: Pick<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "placeholder"> & {
  onSubmit: () => void;
}) {
  return (
    <div className="search-field">
      <Search size={15} />
      <input
        value={value}
        placeholder={placeholder}
        onChange={onChange}
        onKeyDown={(event) => {
          if (event.key === "Enter") onSubmit();
        }}
      />
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  className,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={classNames("empty-state", className)}>
      {icon ? <div className="empty-state-icon">{icon}</div> : null}
      <p>{title}</p>
      {children ? <span>{children}</span> : null}
    </div>
  );
}

export function BrowserLayout({
  sidebar,
  children,
}: {
  sidebar: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="browser-layout">
      <aside className="browser-sidebar">{sidebar}</aside>
      <main className="browser-content">{children}</main>
    </div>
  );
}
