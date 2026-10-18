import {
  Activity,
  BookOpen,
  Boxes,
  Brain,
  ClipboardCheck,
  Coins,
  FileStack,
  FlaskConical,
  FolderKanban,
  GitBranch,
  Gauge,
  History,
  KeyRound,
  Lightbulb,
  Network,
  Scale,
  Settings,
  Stethoscope,
  Telescope,
  Users,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  question: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

// Navigation mirrors the Control Center pages of master §35.
export const NAV: NavGroup[] = [
  {
    label: "Kendali",
    items: [
      { to: "/", label: "Ringkasan", icon: Gauge, question: "Apakah proyek ini dapat berjalan dan apa yang belum siap?" },
      { to: "/readiness", label: "Kesiapan (Doctor)", icon: Stethoscope, question: "Apa yang belum siap dan bagaimana menyelesaikannya?" },
      { to: "/decisions", label: "Keputusan", icon: Scale, question: "Keputusan apa yang menunggu saya?" },
    ],
  },
  {
    label: "Proyek",
    items: [
      { to: "/projects", label: "Portofolio", icon: FolderKanban, question: "Proyek apa saja di domain ini?" },
      { to: "/plans", label: "Rencana", icon: ClipboardCheck, question: "Rencana mana yang perlu ditinjau atau disetujui?" },
      { to: "/dag", label: "Task DAG", icon: GitBranch, question: "Mengapa pekerjaan menunggu dan apa yang terdampak?" },
      { to: "/publications", label: "Dokumen & Publikasi", icon: FileStack, question: "Dokumen/tiket apa yang dipublikasikan dan bagaimana statusnya?" },
    ],
  },
  {
    label: "Workforce",
    items: [
      { to: "/work", label: "Pekerjaan Live", icon: Activity, question: "Apa yang sedang dikerjakan setiap agent?" },
      { to: "/organization", label: "Organisasi & Peran", icon: Users, question: "Fungsi engineering apa yang tersedia dan siapa sedang menjalankannya?" },
      { to: "/evidence", label: "Bukti", icon: BookOpen, question: "Apa bukti kelulusan dan level bukti setiap klaim?" },
    ],
  },
  {
    label: "Sistem",
    items: [
      { to: "/system-map", label: "Peta Sistem", icon: Network, question: "Sistem ini terdiri dari apa dan siapa terhubung dengan siapa?" },
      { to: "/tools", label: "Tools & Repos", icon: Boxes, question: "Tool/repository apa yang dipakai, dari mana asalnya dan untuk apa?" },
      { to: "/models", label: "Model & Rute", icon: Brain, question: "Model mana yang tersedia, diizinkan dan benar-benar mengerjakan tugas?" },
      { to: "/integrations", label: "Integrasi & Akses", icon: KeyRound, question: "Akun/target mana yang terhubung dan hak apa yang dimiliki?" },
      { to: "/costs", label: "Biaya & Kuota", icon: Coins, question: "Biaya apa yang terpakai, diperkirakan dan belum diketahui?" },
    ],
  },
  {
    label: "Evolusi",
    items: [
      { to: "/proposals", label: "Usulan Upgrade", icon: Lightbulb, question: "Bagaimana mengusulkan dan menilai komponen baru?" },
      { to: "/research", label: "Riset", icon: Telescope, question: "Kandidat apa yang ditemukan dan sejauh mana terbukti?" },
      { to: "/evaluation", label: "Evaluasi Model", icon: FlaskConical, question: "Konfigurasi mana yang terbukti terbaik per peran?" },
      { to: "/history", label: "Riwayat & Pemulihan", icon: History, question: "Apa yang berubah, siapa menyetujui dan bagaimana memulihkannya?" },
      { to: "/settings", label: "Pengaturan", icon: Settings, question: "Akun dan preferensi" },
    ],
  },
];
