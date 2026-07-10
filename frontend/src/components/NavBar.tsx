import Link from "next/link";

export default function NavBar() {
  return (
    <nav style={{
      background: "#0f0f0f",
      borderBottom: "1px solid #222",
      padding: "0 1.5rem",
      display: "flex",
      alignItems: "center",
      gap: "1.5rem",
      height: "52px",
    }}>
      <Link href="/" style={{ fontWeight: 700, fontSize: "1rem", color: "#e8d5a3", textDecoration: "none", letterSpacing: "0.05em" }}>
        APEX OCR
      </Link>
      <Link href="/" style={navLink}>Leaderboard</Link>
      <Link href="/matches" style={navLink}>Matches</Link>
      <Link href="/timeline" style={navLink}>Timeline</Link>
    </nav>
  );
}

const navLink: React.CSSProperties = {
  color: "#aaa",
  textDecoration: "none",
  fontSize: "0.875rem",
};
