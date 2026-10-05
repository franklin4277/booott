# Iniciar y detener la plataforma de trading

Abre PowerShell y pega el comando que necesites.

## Iniciar

```powershell
Set-Location 'C:\Users\frank\bottttt\booott'
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
```

El panel de control está disponible en <http://127.0.0.1:8000/dashboard>.

**El trading en vivo está habilitado actualmente en `.env.local`.** Si MT5
está conectado a una cuenta real y una configuración supera las comprobaciones
de IA y riesgo, la plataforma puede enviar una orden real al bróker
automáticamente. Si solo quieres hacer pruebas, verifica que MT5 esté conectado
a la cuenta demo prevista antes de iniciar la plataforma.

## Detener

```powershell
Set-Location 'C:\Users\frank\bottttt\booott'
powershell -ExecutionPolicy Bypass -File .\scripts\stop-all.ps1
```

Detener los servicios no cierra ni modifica las posiciones que ya estén abiertas
en MT5. Gestiona las posiciones existentes desde el terminal de MT5.
