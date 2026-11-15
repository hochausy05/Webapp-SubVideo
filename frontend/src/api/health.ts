export interface HealthResponse {
  status: "ok";
  service: string;
}

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

export async function getBackendHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/health`, {
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(5000),
  });

  if (!response.ok) {
    throw new Error(`Backend returned HTTP ${response.status}.`);
  }

  const result: unknown = await response.json();
  if (
    typeof result !== "object" ||
    result === null ||
    !("status" in result) ||
    result.status !== "ok" ||
    !("service" in result) ||
    typeof result.service !== "string"
  ) {
    throw new Error("Backend returned an unexpected health response.");
  }

  return result as HealthResponse;
}
