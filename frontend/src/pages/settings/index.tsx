import { useAuth } from "../../auth/AuthContext";
import { Card, KeyValue, PageHeader } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import PasswordChangePage from "../auth/PasswordChangePage";

export default function SettingsPage() {
  const { session } = useAuth();
  return (
    <div className="space-y-5">
      <PageHeader title="Pengaturan" question="Akun, sesi dan konteks kerja" />
      <Card title="Akun">
        <KeyValue
          items={[
            ["Username", session?.user.username],
            ["Peran", session?.user.role],
            ["Domain yang diizinkan", session?.allowed_domains.join(", ")],
            ["Domain aktif", session?.context.domain ?? "belum dipilih"],
            ["Sesi berakhir", absoluteTime(session?.expires_at)],
          ]}
        />
      </Card>
      <PasswordChangePage />
    </div>
  );
}
