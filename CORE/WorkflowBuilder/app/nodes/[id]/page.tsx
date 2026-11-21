import { redirect } from "next/navigation";

// GG.UI.2 — node detail moved to the generalized store route. Server
// component: Next 15 passes `params`/`searchParams` as Promises, so we
// await both and forward the query string to the store page.
export default async function NodeDetailRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { id } = await params;
  const sp = await searchParams;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp)) {
    if (v == null) continue;
    if (Array.isArray(v)) v.forEach((x) => qs.append(k, x));
    else qs.append(k, v);
  }
  const query = qs.toString();
  redirect(`/store/node/${id}${query ? `?${query}` : ""}`);
}
