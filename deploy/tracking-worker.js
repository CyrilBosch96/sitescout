/**
 * Click-tracking redirect — SiteScout (2026-08-25)
 *
 * GET /c?p=<notion_page_id>
 *   1. Reads the lead's current Ghost Site Status first (so a lead
 *      already deeper in Track B — Live/Replied/Expired/Converted —
 *      never gets silently reset back to "Selected" by a stray click).
 *   2. Logs the click: Click Count +1, Last Clicked Date = today.
 *   3. If Ghost Site Status was empty, sets it to "Selected" — the exact
 *      same transition the dashboard's manual bulk-select performs
 *      (dashboard/app.py's set_ghost_site_status_selected()) — this is
 *      what auto-starts building that lead's real preview site.
 *   4. Redirects to welcome.example.com regardless of whether the
 *      Notion write succeeded — a tracking failure must never block the
 *      lead from reaching the page they clicked through to.
 *
 * Every lead's link has their own real Notion page ID in the query
 * string (scripts/hp_template.py's tracking_link()) — never one shared
 * link — so a click identifies exactly who clicked, not just a count.
 *
 * Secret required: NOTION_API_KEY, set via
 *   wrangler secret put NOTION_API_KEY
 * (or the Cloudflare dashboard's Worker settings) — never committed here.
 */

const WELCOME_URL = "https://welcome.example.com";
const NOTION_VERSION = "2025-09-03";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/c") {
      return new Response("Not found", { status: 404 });
    }

    const pageId = url.searchParams.get("p");
    if (pageId) {
      try {
        await logClick(pageId, env.NOTION_API_KEY);
      } catch (err) {
        // Swallow — see file header. The redirect below must still fire.
      }
    }

    return Response.redirect(WELCOME_URL, 302);
  },
};

async function logClick(pageId, notionApiKey) {
  const headers = {
    Authorization: `Bearer ${notionApiKey}`,
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
  };

  const getResp = await fetch(`https://api.notion.com/v1/pages/${pageId}`, { headers });
  if (!getResp.ok) return;
  const page = await getResp.json();

  const currentGhostStatus = page.properties?.["Ghost Site Status"]?.select?.name || null;
  const currentClickCount = page.properties?.["Click Count"]?.number || 0;

  const properties = {
    "Click Count": { number: currentClickCount + 1 },
    "Last Clicked Date": { date: { start: new Date().toISOString().slice(0, 10) } },
  };
  if (!currentGhostStatus) {
    properties["Ghost Site Status"] = { select: { name: "Selected" } };
  }

  await fetch(`https://api.notion.com/v1/pages/${pageId}`, {
    method: "PATCH",
    headers,
    body: JSON.stringify({ properties }),
  });
}
