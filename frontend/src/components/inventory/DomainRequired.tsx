import { EmptyState } from "../ui";

/**
 * Domain-first (U-05): inventory and map data are domain-scoped by the backend, which refuses the
 * request until a human picks PERSONAL or OFFICE. Say so plainly instead of showing an error box.
 */
export function DomainRequired() {
  return (
    <EmptyState title="Pilih domain PERSONAL atau OFFICE terlebih dahulu">
      Inventaris dan peta sistem hanya ditampilkan untuk domain yang dipilih manusia secara eksplisit; komponen PERSONAL dan
      OFFICE tidak pernah dicampur. Gunakan pemilih domain di header.
    </EmptyState>
  );
}
