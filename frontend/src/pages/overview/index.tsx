import { EmptyState, PageHeader } from "../../components/ui";

// PLACEHOLDER — replaced by the owning module's implementation.
export default function Page() {
  return (
    <div>
      <PageHeader title="Ringkasan" question="Apakah proyek ini dapat berjalan dan apa yang belum siap?" />
      <EmptyState title="Belum diimplementasikan">Halaman ini belum tersedia pada build ini.</EmptyState>
    </div>
  );
}
