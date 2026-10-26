Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*runserver*' } |
  Select-Object ProcessId, CreationDate, CommandLine |
  Format-Table -AutoSize -Wrap
