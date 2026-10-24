export type Envelope = {
  schema?: string;
  operation?: string;
  ok: boolean;
  result: Record<string, unknown> | null;
  error: { code: string; reason: string; fields?: Record<string, unknown> } | null;
};

export type Bootstrap = {
  roots: string[];
  workflow_root: string;
  time_zones: string[];
  launch_allowed: boolean;
  recording_allowed: boolean;
  reference_prototype: string;
};

export async function bootstrap(): Promise<Bootstrap> {
  const response = await fetch("/api/bootstrap");
  if (!response.ok) throw new Error("bootstrap failed");
  return response.json();
}

export async function call(operation: string, request: Record<string, unknown> = {}): Promise<Envelope> {
  const response = await fetch("/api/workflow", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ operation, request }),
  });
  return response.json();
}
