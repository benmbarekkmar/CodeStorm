from django.shortcuts import render


def dashboard(request):
    return render(request, 'dashboard.html')

def hackathons(request):
    return render(request, "hackathons.html")

def projects(request):
    return render(request, "projects.html")

def teams(request):
    return render(request, "teams.html")


def evaluations(request):
    return render(request, "evaluations.html")