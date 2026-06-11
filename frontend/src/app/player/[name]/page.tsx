import Link from "next/link";
import { api, MatchHistory, PlayerRating } from "@/lib/api";
import { notFound } from "next/navigation";

export const revalidate = 60;

function kd(p: PlayerRating) {
  return p.total_deaths === 0 ? p.total_kills : p.total_kills / p.total_deaths;
}

export default async function PlayerPage({ params }: { params: { name: string } }) {
  const name = decodeURIComponent(params.name);

  let data;
  try {
    data = await api.player(name);
  } catch (e: unknown) {
    if (e instanceof Error && e.message.startsWith("404")) notFound();
    return <p style={{ color: "#f87" }}>Could not reach API.</p>;
  }

  const p = data.player;

  return (
    <>
      <Link href="/" style={{ color: "#888", textDecoration: "none", fontSize: "0.85rem" }}>
        ← Leaderboard
      </Link>

      <h1 style={{ marginTop: "0.75rem", fontSize: "1.5rem" }}>{p.player}</h1>

      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", margin: "1rem 0" }}>
        <StatCard label="ELO" value={Math.round(p.elo)} highlight />
        <StatCard label="Peak ELO" value={Math.round(p.peak_elo)} />
        <StatCard label="Matches" value={p.matches_played} />
        <StatCard label="Kills" value={p.total_kills} />
        <StatCard label="Deaths" value={p.total_deaths} />
        <StatCard label="K/D" value={kd(p).toFixed(2)} />
      </div>

      <h2 style={{ fontSize: "1rem", color: "#aaa", marginTop: "1.5rem" }}>Match History</h2>
      {data.match_history.length === 0 ? (
        <p style={{ color: "#666" }}>No matches recorded.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid #333", color: "#888", textAlign: "left" }}>
              <th style={th}>Date</th>
              <th style={th}>Streamer</th>
              <th style={{ ...th, textAlign: "right" }}>Total Kills</th>
              <th style={{ ...th, textAlign: "right" }}>ELO Change</th>
              <th style={{ ...th, textAlign: "right" }}>ELO After</th>
            </tr>
          </thead>
          <tbody>
            {data.match_history.map((m: MatchHistory) => (
              <tr key={m.match_id} style={{ borderBottom: "1px solid #1a1a1a" }}>
                <td style={td}>
                  <Link href={`/matches/${encodeURIComponent(m.match_id)}`} style={{ color: "#7cf", textDecoration: "none" }}>
                    {new Date(m.start_time).toLocaleDateString()}
                  </Link>
                </td>
                <td style={{ ...td, color: "#aaa" }}>{m.streamer}</td>
                <td style={{ ...td, textAlign: "right" }}>{m.kill_count}</td>
                <td style={{ ...td, textAlign: "right", color: m.elo_change >= 0 ? "#6f6" : "#f86" }}>
                  {m.elo_change >= 0 ? "+" : ""}{Math.round(m.elo_change)}
                </td>
                <td style={{ ...td, textAlign: "right", fontWeight: 600 }}>
                  {Math.round(m.elo_after)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function StatCard({ label, value, highlight }: { label: string; value: string | number; highlight?: boolean }) {
  return (
    <div style={{
      background: "#111", border: "1px solid #222", borderRadius: 6,
      padding: "0.75rem 1.25rem", minWidth: "80px", textAlign: "center",
    }}>
      <div style={{ fontSize: "1.4rem", fontWeight: 700, color: highlight ? "#ffd700" : "#e0e0e0" }}>
        {value}
      </div>
      <div style={{ fontSize: "0.75rem", color: "#888", marginTop: 2 }}>{label}</div>
    </div>
  );
}

const th: React.CSSProperties = { padding: "6px 8px", fontWeight: 500 };
const td: React.CSSProperties = { padding: "7px 8px" };
