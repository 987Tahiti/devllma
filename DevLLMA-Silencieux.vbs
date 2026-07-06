' Lance le serveur DevLLMA en arriere-plan, SANS aucune fenetre (pythonw).
' Double-clic pour demarrer, ou place ce fichier dans le dossier Demarrage
' de Windows pour un lancement automatique a chaque ouverture de session.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Devllma"
sh.Run """C:\Users\Admin\AppData\Local\Programs\Python\Python311\pythonw.exe"" ""C:\Devllma\webui.py""", 0, False
