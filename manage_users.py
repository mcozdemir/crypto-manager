# -*- coding: utf-8 -*-
"""
manage_users.py
---------------
Panel kullanıcılarını yönetir. Kullanıcılar .env içindeki (Turso)
veritabanına kaydedilir; yerelde eklenen kullanıcı yayındaki sitede de
geçerlidir.

Kullanım (proje klasöründe):
    .venv/bin/python manage_users.py add <kullanici>      # yeni kullanıcı + QR kod
    .venv/bin/python manage_users.py list                 # kullanıcıları listele
    .venv/bin/python manage_users.py reset <kullanici>    # Authenticator kaydını yenile (telefon değişti vb.)
    .venv/bin/python manage_users.py disable <kullanici>  # girişini geçici kapat
    .venv/bin/python manage_users.py enable <kullanici>   # tekrar aç
    .venv/bin/python manage_users.py delete <kullanici>   # kalıcı sil
    .venv/bin/python manage_users.py code <kullanici>     # (test) şu anki kodu göster
"""

import sys

import auth
import db


def show_setup(username: str, secret: str):
    uri = auth.provisioning_uri(username, secret)
    print()
    print(f"'{username}' için Authenticator kurulumu")
    print("-" * 60)
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=2)
        qr.add_data(uri)
        qr.make(fit=True)
        print("Telefondaki Authenticator uygulamasıyla bu QR kodu okutun:\n")
        qr.print_ascii(invert=True)
    except ImportError:
        print("(QR kod göstermek için: .venv/bin/pip install qrcode)")
    print("QR okutulamazsa uygulamada 'Kurulum anahtarı gir' seçip şunu yazın:")
    print(f"  Hesap adı : {auth.ISSUER} ({username})")
    print(f"  Anahtar   : {secret}")
    print("  Tür       : Zamana dayalı (TOTP), 6 hane, 30 sn")
    print("-" * 60)
    print("Bu anahtarı yalnızca kullanıcının kendisiyle paylaşın.\n")


def main(argv) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    name = argv[2] if len(argv) > 2 else None
    target = db.get_db()
    print(f"Veritabanı: {target.describe()}")
    if target.kind != "turso":
        print("UYARI: Turso tanımlı değil; kullanıcı yalnızca yerel veritabanına eklenecek "
              "ve yayındaki sitede geçerli OLMAYACAK.")

    try:
        if cmd == "list":
            users = auth.list_users()
            if not users:
                print("Henüz kullanıcı yok.")
            for u in users:
                status = "aktif" if u["is_active"] else "KAPALI"
                print(f"  {u['username']:<20} {status:<7} oluşturma: {u['created_at']}  "
                      f"son giriş: {u['last_login_at'] or '-'}")
            return 0

        if not name:
            print("Kullanıcı adı gerekli. Örn: manage_users.py add mehmet")
            return 1

        if cmd == "add":
            show_setup(auth.normalize_username(name), auth.create_user(name))
        elif cmd == "reset":
            show_setup(auth.normalize_username(name), auth.reset_user_secret(name))
            print("Eski Authenticator kaydı artık çalışmaz; uygulamadan silebilirsiniz.")
        elif cmd == "disable":
            auth.set_user_active(name, False)
            print(f"'{name}' girişi kapatıldı.")
        elif cmd == "enable":
            auth.set_user_active(name, True)
            print(f"'{name}' girişi açıldı.")
        elif cmd == "delete":
            if input(f"'{name}' kalıcı olarak silinsin mi? (evet/hayır): ").strip().lower() != "evet":
                print("İptal edildi.")
                return 1
            auth.delete_user(name)
            print(f"'{name}' silindi.")
        elif cmd == "code":
            user = auth.get_user(name)
            if not user:
                raise ValueError(f"'{name}' kullanıcısı bulunamadı.")
            print(f"Şu anki kod: {auth.current_code(user['totp_secret'])}")
        else:
            print(f"Bilinmeyen komut: {cmd}\n{__doc__}")
            return 1
    except (ValueError, db.DatabaseError) as exc:
        print(f"HATA: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
