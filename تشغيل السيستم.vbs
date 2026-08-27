' تشغيل النظام المحاسبي بدون أي نافذة + فتح المتصفح تلقائيًا
' محاسب / أحمد عبدالله
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)

' هل السيستم شغال بالفعل؟
running = False
On Error Resume Next
Set xhr = CreateObject("MSXML2.ServerXMLHTTP.6.0")
xhr.Open "GET", "http://localhost:5000/login", False
xhr.setTimeouts 1500, 1500, 1500, 1500
xhr.Send
If Err.Number = 0 Then
    If xhr.Status = 200 Then running = True
End If
Err.Clear
On Error GoTo 0

If Not running Then
    shell.CurrentDirectory = folder
    ' pythonw = بايثون بدون نافذة سوداء نهائيًا (نافذة مخفية 0)
    shell.Run "pythonw app.py", 0, False
    ' استنى لحد ما السيستم يقوم (أقصى 15 ثانية)
    ready = False
    For i = 1 To 30
        WScript.Sleep 500
        On Error Resume Next
        Set x2 = CreateObject("MSXML2.ServerXMLHTTP.6.0")
        x2.Open "GET", "http://localhost:5000/login", False
        x2.setTimeouts 1000, 1000, 1000, 1000
        x2.Send
        If Err.Number = 0 Then
            If x2.Status = 200 Then ready = True
        End If
        Err.Clear
        On Error GoTo 0
        If ready Then Exit For
    Next
End If

shell.Run "http://localhost:5000"
