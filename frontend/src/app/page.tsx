import Link from "next/link";
import { api, PlayerRating } from "@/lib/api";

export const revalidate = 60;

function eloColor(elo: number) {
  if (elo >= 1400) return "#ffd700";
  if (elo >= 1200) return "#c0c0c0";
  if (elo >= 1050) return "#cd7f32";
  return "#888";
}

function kd(p: PlayerRating) {
  return p.total_deaths === 0 ? p.total_kills : p.total_kills / p.total_deaths;
}

export default async function LeaderboardPage({
  searchParams,
}: {
  searchParams: { page?: string; min?: string };
}) {
  const page = Math.max(1, parseInt(searchParams.page ?? "1", 10));
  const min = parseInt(searchParams.min ?? "3", 10);
  const limit = 50;
  const offset = (page - 1) * limit;

  let data;
  try {
    data = await api.rankings({ min_matches: min, limit, offset });
  } catch {
    return <p style={{ color: "#f87" }}>Could not reach API. Is the tunnel running?</p>;
  }

  const totalPages = Math.max(1, Math.ceil(data.total / limit));

  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", gap: "1rem", marginBottom: "1rem", flexWrap: "wrap" }}>
        <h1 style={{ margin: 0, fontSize: "1.4rem" }}>ELO Leaderboard</h1>
        <span style={{ color: "#888", fontSize: "0.85rem" }}>{data.total} players (min {min} matches)</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: "0.5rem" }}>
          {[1, 3, 5].map((m) => (
            <Link key={m} href={`/?min=${m}`} style={{
              padding: "2px 8px", borderRadius: 4, fontSize: "0.8rem",
              background: m === min ? "#333" : "transparent",
              border: "1px solid #333", color: "#ccc", textDecoration: "none",
            }}>
              {m}+ matches
            </Link>
          ))}
        </div>
      </div>

      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
        <thead>
          <tr style={{ borderBottom: "1px solid #333", color: "#888", textAlign: "left" }}>
            <th style={th}>#</th>
            <th style={th}>Player</th>
            <th style={{ ...th, textAlign: "right" }}>ELO</th>
            <th style={{ ...th, textAlign: "right" }}>Matches</th>
            <th style={{ ...th, textAlign: "right" }}>Kills</th>
            <th style={{ ...th, textAlign: "right" }}>Deaths</th>
            <th style={{ ...th, textAlign: "right" }}>K/D</th>
          </tr>
        </thead>
        <tbody>
          {data.players.map((p: PlayerRating, i: number) => (
            <tr key={p.player} style={{ borderBottom: "1px solid #1a1a1a" }}>
              <td style={td}>{offset + i + 1}</td>
              <td style={td}>
                <Link href={`/player/${encodeURIComponent(p.player)}`} style={{ color: "#e8d5a3", textDecoration: "none" }}>
                  {p.player}
                </Link>
              </td>
              <td style={{ ...td, textAlign: "right", fontWeight: 600, color: eloColor(p.elo) }}>
                {Math.round(p.elo)}
              </td>
              <td style={{ ...td, textAlign: "right", color: "#aaa" }}>{p.matches_played}</td>
              <td style={{ ...td, textAlign: "right" }}>{p.total_kills}</td>
              <td style={{ ...td, textAlign: "right" }}>{p.total_deaths}</td>
              <td style={{ ...td, textAlign: "right", color: kd(p) >= 2 ? "#7cf" : "#ccc" }}>
                {kd(p).toFixed(2)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ display: "flex", gap: "0.5rem", marginTop: "1.5rem", justifyContent: "center" }}>
        {page > 1 && (
          <Link href={`/?page=${page - 1}&min=${min}`} style={pageBtn}>← Prev</Link>
        )}
        <span style={{ padding: "4px 12px", color: "#888", fontSize: "0.85rem" }}>
          Page {page} / {totalPages}
        </span>
        {page < totalPages && (
          <Link href={`/?page=${page + 1}&min=${min}`} style={pageBtn}>Next →</Link>
        )}
      </div>
    </>
  );
}

const th: React.CSSProperties = { padding: "6px 8px", fontWeight: 500 };
const td: React.CSSProperties = { padding: "7px 8px" };
const pageBtn: React.CSSProperties = {
  padding: "4px 14px", borderRadius: 4, border: "1px solid #333",
  color: "#ccc", textDecoration: "none", fontSize: "0.85rem", background: "#111",
};
