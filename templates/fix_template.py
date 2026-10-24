import re

with open("C:\\Users\\oussa.LAPTOP-THCQB19P\\.gemini\\antigravity\\scratch\\start-smart-frontend\\CodeStorm\\templates\\evaluations.html", "r", encoding="utf-8") as f:
    content = f.read()

# Fix the main issue on line 882: replace {{% endif with {% endif
content = content.replace('{{% endif', '{% endif')

# Also fix any other similar issues
content = content.replace('{{ if', '{% if')
content = content.replace('{{ endif', '{% endif')

with open("C:\\Users\\oussa.LAPTOP-THCQB19P\\.gemini\\antigravity\\scratch\\start-smart-frontend\\CodeStorm\\templates\\evaluations.html", "w", encoding="utf-8") as f:
    f.write(content)

print("Template file fixed!")