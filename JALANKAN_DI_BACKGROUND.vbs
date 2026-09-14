Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' 0 = Sembunyikan jendela CMD, False = Jalankan async di background
WshShell.Run "cmd /c START_BOT.bat", 0, False

MsgBox "🚀 Bot OriontClipper telah berhasil dijalankan di background!" & vbCrLf & vbCrLf & _
       "• Bot aktif dan siap menerima video/link di Telegram." & vbCrLf & _
       "• Tidak ada jendela CMD yang mengganggu layar." & vbCrLf & _
       "• Untuk mematikan bot, klik dua kali file STOP_BOT.bat", _
       vbInformation, "OriontClipper - Background Runner"
