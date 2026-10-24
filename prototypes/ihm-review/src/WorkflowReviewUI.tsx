import type { ReactNode } from 'react';
export function Button({ children, onClick, primary = false, disabled = false }: { children: ReactNode; onClick?: () => void; primary?: boolean; disabled?: boolean }) { return <button className={'button ' + (primary ? 'primary' : '')} onClick={onClick} disabled={disabled}>{children}</button>; }
export function Tag({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={'badge ' + tone}>{children}</span>; }
export function Panel({ title, sub, children, action, className = '' }: { title: string; sub?: string; children: ReactNode; action?: ReactNode; className?: string }) { return <section className={'panel ' + className}><div className="panel-head"><div><h3>{title}</h3>{sub && <p>{sub}</p>}</div>{action}</div>{children}</section>; }
export function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
export function Pair({ label, children }: { label: string; children: ReactNode }) { return <div><dt>{label}</dt><dd>{children}</dd></div>; }
export function Notice({ title, children, tone = '' }: { title: string; children?: ReactNode; tone?: string }) { return <div className={'r-banner ' + tone}><strong>{title}</strong>{children && <span>{children}</span>}</div>; }
