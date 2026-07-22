// Regenera images/projects/resumen.svg y toplangs.svg con datos reales de la API de GitHub.
// Requiere STATS_TOKEN: un PAT clásico con scopes `repo` + `read:user` (el GITHUB_TOKEN
// por defecto de Actions no puede ver repos privados ni contributionsCollection completo).

const TOKEN = process.env.STATS_TOKEN;
const USERNAME = "MILLERMARRU";
const FEATURED_PROJECTS_COUNT = 7; // proyectos destacados mostrados arriba en el README

if (!TOKEN) {
  console.error("Falta el secret STATS_TOKEN.");
  process.exit(1);
}

async function gql(query, variables = {}) {
  const res = await fetch("https://api.github.com/graphql", {
    method: "POST",
    headers: {
      Authorization: `bearer ${TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query, variables }),
  });
  const json = await res.json();
  if (json.errors) throw new Error(JSON.stringify(json.errors));
  return json.data;
}

function escapeXml(str) {
  return String(str).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

async function fetchCreatedAt() {
  const data = await gql(`{ user(login: "${USERNAME}") { createdAt } }`);
  return new Date(data.user.createdAt);
}

async function fetchRepoStats() {
  let cursor = null;
  let hasNextPage = true;
  let totalCount = 0;
  const langCounts = {};

  while (hasNextPage) {
    const data = await gql(
      `query($cursor: String) {
        user(login: "${USERNAME}") {
          repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes { primaryLanguage { name } }
          }
        }
      }`,
      { cursor }
    );
    const conn = data.user.repositories;
    totalCount = conn.totalCount;
    for (const node of conn.nodes) {
      const lang = node.primaryLanguage?.name;
      if (lang) langCounts[lang] = (langCounts[lang] || 0) + 1;
    }
    hasNextPage = conn.pageInfo.hasNextPage;
    cursor = conn.pageInfo.endCursor;
  }

  return { totalCount, langCounts };
}

async function fetchTotalCommits(createdAt) {
  let total = 0;
  const start = new Date(createdAt);
  const now = new Date();

  let from = new Date(start);
  while (from < now) {
    let to = new Date(from);
    to.setUTCFullYear(to.getUTCFullYear() + 1);
    if (to > now) to = now;

    const data = await gql(
      `query($from: DateTime!, $to: DateTime!) {
        user(login: "${USERNAME}") {
          contributionsCollection(from: $from, to: $to) {
            totalCommitContributions
            restrictedContributionsCount
          }
        }
      }`,
      { from: from.toISOString(), to: to.toISOString() }
    );
    const c = data.user.contributionsCollection;
    total += c.totalCommitContributions + c.restrictedContributionsCount;
    from = to;
  }
  return total;
}

function formatCommits(n) {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k+`;
  return `${n}`;
}

function yearsActive(createdAt) {
  const years = (Date.now() - new Date(createdAt).getTime()) / (1000 * 60 * 60 * 24 * 365.25);
  return `${Math.floor(years)}+`;
}

function buildResumenSvg({ repos, commits, createdAt }) {
  const stats = [
    { value: `${repos}`, label: "Repos", color: "#38BDF8" },
    { value: formatCommits(commits), label: "Commits", color: "#34D399" },
    { value: `${FEATURED_PROJECTS_COUNT}`, label: "En producción", color: "#F59E0B" },
    { value: yearsActive(createdAt), label: "Años activo", color: "#A78BFA" },
  ];

  const blockWidth = 190;
  const dividers = [1, 2, 3]
    .map(
      (i) =>
        `<line x1="${i * blockWidth}" y1="26" x2="${i * blockWidth}" y2="84" stroke="#30363d" stroke-width="1"/>`
    )
    .join("\n");

  const blocks = stats
    .map((s, i) => {
      const cx = blockWidth * i + blockWidth / 2;
      return `<text x="${cx}" y="56" text-anchor="middle" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="26" font-weight="700" fill="${s.color}">${escapeXml(s.value)}</text>
<text x="${cx}" y="78" text-anchor="middle" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="11" fill="#9ca3af" letter-spacing="0.3">${escapeXml(s.label)}</text>`;
    })
    .join("\n");

  return `<svg width="760" height="110" viewBox="0 0 760 110" xmlns="http://www.w3.org/2000/svg">
<rect x="0.5" y="0.5" width="759" height="109" rx="14" fill="#0d1117" stroke="#30363d"/>
${dividers}
${blocks}
</svg>
`;
}

function buildTopLangsSvg(langCounts) {
  const palette = {
    TypeScript: "#3178C6",
    JavaScript: "#F7DF1E",
    Java: "#ED8B00",
    Python: "#3776AB",
    CSS: "#1572B6",
    HTML: "#E34F26",
    PHP: "#777BB4",
    Astro: "#FF5D01",
    Kotlin: "#7F52FF",
    Shell: "#89E051",
  };
  const fallback = "#94A3B8";

  const top = Object.entries(langCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  const max = top.length ? top[0][1] : 1;
  const trackWidth = 190;
  const rowHeight = 28;
  const startY = 60;
  const height = startY + (top.length - 1) * rowHeight + 34;

  const rows = top
    .map(([lang, count], i) => {
      const y = startY + i * rowHeight;
      const barWidth = Math.max(4, Math.round((count / max) * trackWidth));
      const color = palette[lang] || fallback;
      return `<text x="20" y="${y}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12.5" fill="#e6edf3">${escapeXml(lang)}</text>
<rect x="110" y="${y - 8}" width="${trackWidth}" height="8" rx="4" fill="#21262d"/>
<rect x="110" y="${y - 8}" width="${barWidth}" height="8" rx="4" fill="${color}"/>
<text x="310" y="${y}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="11.5" fill="#9ca3af">${count}</text>`;
    })
    .join("\n");

  return `<svg width="380" height="${height}" viewBox="0 0 380 ${height}" xmlns="http://www.w3.org/2000/svg">
<rect x="0.5" y="0.5" width="379" height="${height - 1}" rx="14" fill="#0d1117" stroke="#30363d"/>
<text x="20" y="30" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12.5" font-weight="600" fill="#9ca3af" letter-spacing="0.4">TOP LANGUAGES · POR REPOS</text>
${rows}
</svg>
`;
}

const fs = await import("node:fs/promises");

const createdAt = await fetchCreatedAt();
const { totalCount, langCounts } = await fetchRepoStats();
const totalCommits = await fetchTotalCommits(createdAt);

const resumenSvg = buildResumenSvg({ repos: totalCount, commits: totalCommits, createdAt });
const toplangsSvg = buildTopLangsSvg(langCounts);

await fs.writeFile("images/projects/resumen.svg", resumenSvg);
await fs.writeFile("images/projects/toplangs.svg", toplangsSvg);

console.log(`Repos: ${totalCount} | Commits: ${totalCommits} | Años activo: ${yearsActive(createdAt)}`);
console.log("Lenguajes:", langCounts);
