"""Deterministic demo data: ~100 Confluence pages and ~60 Jira issues with links.

Used for local testing without access to Jira/Confluence:
    python app.py seed-demo [--reset]
Everything goes through db.save_document, i.e. exactly the same storage path
(chunks, FTS, edges, reference nodes) as a real crawl.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db
from .links import LinkContext, confluence_page_url, jira_issue_url
from .models import (
    EDGE_JIRA_PARENT_OF, EDGE_JIRA_RELATES_TO, EDGE_LINKS_TO, EDGE_LINKS_TO_CONFLUENCE,
    EDGE_MENTIONS_JIRA, EDGE_PARENT_OF, STATE_FORBIDDEN, STATE_MISSING, FetchedDocument,
    LinkTarget, confluence_uid, jira_uid, unresolved_page_uid,
)
from .text import chunk_sections

DEMO_CONFLUENCE_BASE = "https://confluence.demo.intern"
DEMO_JIRA_BASE = "https://jira.demo.intern"
FIRST_PAGE_ID = 100001

# --------------------------------------------------------------------------- pages
# (title, description, keywords, links_to (titles), jira keys, children)

P = tuple

ENG_TREE: P = ("Engineering Startseite", "Einstieg in alle Engineering-Themen: Architektur, Prozesse, Entscheidungen und Onboarding.",
    ["Engineering", "Architektur", "Prozesse"], ["Glossar", "Roadmap 2026"], [], [
    ("Architektur-Übersicht", "Die Plattform besteht aus fachlich geschnittenen Microservices hinter einem API-Gateway; die Kommunikation erfolgt synchron per REST und asynchron über Kafka.",
        ["Microservices", "API-Gateway", "Kafka", "PostgreSQL"], ["ADR-001 Microservices statt Monolith", "Kubernetes-Cluster"], ["ENG-2"], [
        ("Backend-Services", "Übersicht aller Backend-Services mit Verantwortlichkeiten, Schnittstellen und Betriebsdaten.",
            ["Services", "REST", "Schnittstellen"], ["Datenmodell"], [], [
            ("Auth-Service", "Der Auth-Service kapselt Anmeldung, Token-Ausgabe und Rechteprüfung auf Basis von OAuth2 und OpenID Connect mit Keycloak.",
                ["OAuth2", "OpenID Connect", "Keycloak", "Token", "Session"], ["ADR-004 OAuth2 mit Keycloak", "SSO-Login", "Zugriffskonzept"], ["ENG-1", "ENG-9", "SEC-1"], []),
            ("Billing-Service", "Der Billing-Service erzeugt Rechnungen, verarbeitet Zahlungen per SEPA-Lastschrift und meldet Zahlungsstatus an andere Services.",
                ["Rechnung", "SEPA", "Zahlung", "Webhook"], ["Rechnungsportal", "Event-Streaming mit Kafka"], ["ENG-5", "ENG-29"], []),
            ("Search-Service", "Der Search-Service stellt die Volltextsuche über Dokumente, Tickets und Rechnungen bereit und pflegt den Suchindex inkrementell.",
                ["Volltextsuche", "Suchindex", "Relevanz", "Ranking"], ["Volltextsuche", "ADR-002 PostgreSQL als Primärdatenbank"], ["ENG-2", "ENG-13", "ENG-15"], []),
            ("Notification-Service", "Der Notification-Service versendet E-Mails, Push-Nachrichten und Webhooks und konsumiert dafür Events aus Kafka.",
                ["E-Mail", "Push", "Webhooks", "Benachrichtigung"], ["Benachrichtigungscenter", "Event-Streaming mit Kafka"], ["ENG-4", "ENG-24"], []),
            ("API-Gateway", "Das API-Gateway terminiert TLS, routet Anfragen an die Services und setzt Rate Limits pro Mandant durch.",
                ["Rate Limiting", "Routing", "TLS", "Latenz"], ["Zertifikatserneuerung", "ADR-007 API-Versionierung"], ["OPS-7", "SEC-8"], []),
            ("Reporting-Service", "Der Reporting-Service erstellt Auswertungen und Exporte als CSV und stellt Kennzahlen für Dashboards bereit.",
                ["Reporting", "CSV", "Export", "Kennzahlen"], ["Datenexport", "Mandanten-Dashboard"], ["ENG-3", "ENG-16"], []),
        ]),
        ("Frontend", "Überblick über die Clients: Web-App, Mobile-App und das gemeinsame Design-System.",
            ["Frontend", "Clients", "UI"], ["Design-System"], [], [
            ("Web-App", "Die Web-App ist eine TypeScript-Single-Page-Application mit Fokus auf Barrierefreiheit und schneller Ladezeit.",
                ["TypeScript", "SPA", "Barrierefreiheit", "Ladezeit"], ["Design-System", "SSO-Login"], ["ENG-8"], []),
            ("Mobile-App", "Die Mobile-App für iOS und Android unterstützt einen Offline-Modus und Push-Benachrichtigungen.",
                ["iOS", "Android", "Offline-Modus", "Push"], ["Notification-Service"], ["ENG-10", "ENG-23"], []),
            ("Design-System", "Das Design-System definiert Komponenten, Farben, Typografie und Interaktionsmuster für alle Oberflächen.",
                ["Komponenten", "Farben", "Typografie"], ["Web-App"], [], []),
        ]),
        ("Datenmodell", "Das relationale Datenmodell liegt in PostgreSQL; Schemaänderungen erfolgen ausschließlich über versionierte Migrationen.",
            ["PostgreSQL", "Migrationen", "Schema", "Mandant"], ["ADR-002 PostgreSQL als Primärdatenbank", "ADR-008 Mandantenfähigkeit", "PostgreSQL-Cluster"], [], []),
        ("Event-Streaming mit Kafka", "Domänenereignisse werden über Kafka-Topics verteilt; Consumer müssen idempotent sein und Retries tolerieren.",
            ["Kafka", "Topics", "Consumer", "Retention", "Idempotenz"], ["ADR-003 Kafka für asynchrone Kommunikation", "Kafka-Consumer-Lag beheben", "Kafka-Cluster"], ["ENG-17", "ENG-21"], []),
    ]),
    ("Architecture Decision Records", "Sammlung aller Architekturentscheidungen mit Kontext, Entscheidung und Konsequenzen.",
        ["ADR", "Entscheidung", "Architektur"], [], [], [
        ("ADR-001 Microservices statt Monolith", "Wir schneiden die Plattform in fachliche Services, um Teams unabhängig deployen zu lassen.",
            ["Microservices", "Monolith", "Deployment"], ["Architektur-Übersicht"], [], []),
        ("ADR-002 PostgreSQL als Primärdatenbank", "PostgreSQL ist die Standarddatenbank für alle Services; Ausnahmen müssen begründet werden.",
            ["PostgreSQL", "Datenbank", "Transaktionen"], ["Datenmodell", "PostgreSQL-Cluster"], [], []),
        ("ADR-003 Kafka für asynchrone Kommunikation", "Asynchrone Kommunikation zwischen Services läuft über Kafka statt über direkte Aufrufe.",
            ["Kafka", "asynchron", "Events"], ["Event-Streaming mit Kafka"], ["ENG-17"], []),
        ("ADR-004 OAuth2 mit Keycloak", "Authentifizierung und Single Sign-On werden zentral mit Keycloak über OAuth2 und OpenID Connect umgesetzt.",
            ["Keycloak", "OAuth2", "SSO"], ["Auth-Service", "SSO-Login"], ["ENG-1"], []),
        ("ADR-005 Feature-Flags", "Neue Funktionen werden hinter Feature-Flags ausgeliefert und schrittweise aktiviert.",
            ["Feature-Flags", "Rollout", "Release"], ["Release-Prozess"], [], []),
        ("ADR-006 Observability mit OpenTelemetry", "Traces, Metriken und Logs werden einheitlich mit OpenTelemetry erfasst.",
            ["OpenTelemetry", "Tracing", "Metriken", "Logs"], ["Grafana-Dashboards", "Log-Analyse"], ["OPS-12"], []),
        ("ADR-007 API-Versionierung", "Öffentliche APIs werden über den URL-Pfad versioniert; alte Versionen bleiben zwölf Monate verfügbar.",
            ["API", "Versionierung", "Abwärtskompatibilität"], ["API-Gateway"], [], []),
        ("ADR-008 Mandantenfähigkeit", "Mandanten werden logisch über eine Mandanten-ID in jeder Tabelle getrennt; Row-Level-Security ist Pflicht.",
            ["Mandant", "Row-Level-Security", "Isolation"], ["Datenmodell", "Zugriffskonzept"], ["SEC-7"], []),
    ]),
    ("Entwicklungsprozess", "Wie wir entwickeln: Reviews, Branches, Releases, Tests und Pipeline.",
        ["Prozess", "Qualität", "Release"], [], [], [
        ("Code-Review-Richtlinien", "Jede Änderung wird von mindestens einer weiteren Person geprüft; Sicherheitsaspekte sind Teil jedes Reviews.",
            ["Code-Review", "Pull Request", "Vier-Augen-Prinzip"], ["Secure Coding", "Definition of Done"], [], []),
        ("Branching-Strategie", "Wir arbeiten trunk-based mit kurzlebigen Feature-Branches und verpflichtenden Pull Requests.",
            ["Git", "Trunk-based", "Feature-Branch"], ["Release-Prozess"], [], []),
        ("Release-Prozess", "Releases werden automatisiert über die CI/CD-Pipeline ausgerollt; ein Rollback ist jederzeit möglich.",
            ["Release", "Rollout", "Rollback", "Changelog"], ["Deployment-Rollback", "CI/CD-Pipeline", "ADR-005 Feature-Flags"], ["OPS-13"], []),
        ("Definition of Done", "Eine Story ist fertig, wenn Code, Tests, Dokumentation und Review abgeschlossen und das Feature ausgerollt ist.",
            ["Definition of Done", "Akzeptanzkriterien", "Tests"], ["Teststrategie"], [], []),
        ("Teststrategie", "Die Testpyramide umfasst Unit-, Integrations- und End-to-End-Tests; kritische Pfade werden zusätzlich als Lasttest geprüft.",
            ["Unit-Tests", "Integrationstests", "End-to-End", "Lasttest"], ["CI/CD-Pipeline"], [], []),
        ("CI/CD-Pipeline", "Die Pipeline baut, testet, scannt Abhängigkeiten und deployt Container-Images nach Kubernetes.",
            ["CI/CD", "Pipeline", "Container", "Abhängigkeiten"], ["Kubernetes-Cluster", "Vulnerability-Management"], ["SEC-3", "SEC-4"], []),
    ]),
    ("Onboarding für Entwickler", "Alles für die ersten Tage im Engineering-Team.",
        ["Onboarding", "Einarbeitung"], ["Zugänge beantragen"], [], [
        ("Entwicklungsumgebung einrichten", "Schritt-für-Schritt-Anleitung für Laptop, Repositories, lokale Datenbank und Zertifikate des internen Netzes.",
            ["Entwicklungsumgebung", "Repository", "lokale Datenbank", "Zertifikat"], ["Branching-Strategie"], [], []),
        ("Erste Woche", "Ablauf der ersten Woche: Buddy, Architektur-Einführung, erster kleiner Pull Request.",
            ["Buddy", "Einarbeitung", "Pull Request"], ["Architektur-Übersicht", "Code-Review-Richtlinien"], [], []),
        ("Glossar", "Begriffe und Abkürzungen, die im Team regelmäßig verwendet werden, von ADR bis SLO.",
            ["Glossar", "Begriffe", "Abkürzungen"], ["SLOs und Fehlerbudgets"], [], []),
        ("Zugänge beantragen", "Welche Zugänge benötigt werden (VPN, Git, Jira, Confluence, Kubernetes) und wie sie beantragt werden.",
            ["VPN", "Zugang", "Berechtigung", "Antrag"], ["Zugriffskonzept"], [], []),
    ]),
])

OPS_TREE: P = ("Betrieb Startseite", "Einstieg für den Betrieb: Runbooks, Monitoring, Postmortems und Infrastruktur.",
    ["Betrieb", "Operations", "Runbook"], ["On-Call-Handbuch"], [], [
    ("Runbooks", "Standardisierte Anleitungen für wiederkehrende Betriebsaufgaben und Störungen.",
        ["Runbook", "Störung", "Anleitung"], [], [], [
        ("Datenbank-Failover", "So wird bei einem Ausfall der primären PostgreSQL-Instanz kontrolliert auf das Replikat umgeschaltet.",
            ["Failover", "PostgreSQL", "Replikat", "Patroni"], ["PostgreSQL-Cluster", "Backup und Restore"], ["OPS-5", "OPS-10"], []),
        ("Deployment-Rollback", "Rücknahme eines fehlerhaften Deployments über die Pipeline oder manuell per Helm.",
            ["Rollback", "Deployment", "Helm", "Kubernetes"], ["Release-Prozess", "Kubernetes-Cluster"], ["OPS-1"], []),
        ("Zertifikatserneuerung", "Erneuerung interner TLS-Zertifikate der eigenen CA inklusive Verteilung an Gateway und Services.",
            ["Zertifikat", "TLS", "interne CA", "Ablaufdatum"], ["API-Gateway", "Verschlüsselung"], ["OPS-3", "OPS-9"], []),
        ("Log-Analyse", "Wie Logs zentral gesucht, gefiltert und mit Traces korreliert werden.",
            ["Logs", "Korrelation", "Trace-ID"], ["ADR-006 Observability mit OpenTelemetry", "Grafana-Dashboards"], [], []),
        ("Backup und Restore", "Tägliche Backups aller Datenbanken, Aufbewahrung und Ablauf eines vollständigen Restores.",
            ["Backup", "Restore", "Aufbewahrung", "Verschlüsselung"], ["PostgreSQL-Cluster", "Löschkonzept"], ["OPS-8", "OPS-14", "SEC-6"], []),
        ("Skalierung bei Lastspitzen", "Maßnahmen bei Lastspitzen: horizontale Skalierung, Rate Limits anpassen, Caches vorwärmen.",
            ["Skalierung", "Lastspitze", "Autoscaling", "Cache"], ["API-Gateway", "Kubernetes-Cluster"], ["OPS-7"], []),
        ("Kafka-Consumer-Lag beheben", "Diagnose und Abbau von Consumer-Lag, inklusive Neustart und Partition-Rebalancing.",
            ["Kafka", "Consumer-Lag", "Partition", "Rebalancing"], ["Kafka-Cluster", "Event-Streaming mit Kafka"], ["OPS-2", "OPS-4", "OPS-11"], []),
        ("Speicherplatz bereinigen", "Vorgehen, wenn Volumes oder Log-Server vollaufen: Rotation, Komprimierung, Aufräumjobs.",
            ["Speicherplatz", "Log-Rotation", "Volume"], ["Storage"], ["OPS-6"], []),
    ]),
    ("Monitoring und Alerting", "Wie wir die Plattform überwachen und wann wer alarmiert wird.",
        ["Monitoring", "Alerting", "Metriken"], [], [], [
        ("Grafana-Dashboards", "Übersicht der wichtigsten Dashboards für Latenz, Fehlerraten, Sättigung und Geschäftskennzahlen.",
            ["Grafana", "Dashboard", "Latenz", "Fehlerrate"], ["SLOs und Fehlerbudgets"], ["OPS-12"], []),
        ("Alert-Regeln", "Alle Alert-Regeln mit Schwellwerten, Priorität und zuständigem Team.",
            ["Alert", "Schwellwert", "Priorität"], ["Eskalationsstufen"], ["OPS-11"], []),
        ("SLOs und Fehlerbudgets", "Service Level Objectives je Service und wie Fehlerbudgets Releases steuern.",
            ["SLO", "Fehlerbudget", "Verfügbarkeit"], ["Release-Prozess"], [], []),
    ]),
    ("Incident-Postmortems", "Schuldfreie Nachbetrachtungen aller größeren Störungen mit Maßnahmen.",
        ["Postmortem", "Incident", "Maßnahmen"], [], [], [
        ("Postmortem 2026-01 Login-Ausfall", "Nach einem fehlerhaften Keycloak-Update war die Anmeldung 47 Minuten lang nicht möglich.",
            ["Login", "Keycloak", "Ausfall", "Update"], ["Auth-Service", "Deployment-Rollback"], ["OPS-1", "ENG-7"], []),
        ("Postmortem 2026-02 Zahlungsverzögerung", "Zahlungseingänge wurden wegen eines blockierten Kafka-Consumers mit bis zu sechs Stunden Verzögerung verbucht.",
            ["Zahlung", "Verzögerung", "Kafka", "Consumer"], ["Billing-Service", "Kafka-Consumer-Lag beheben"], ["OPS-2", "ENG-29"], []),
        ("Postmortem 2026-03 Zertifikat abgelaufen", "Ein abgelaufenes internes TLS-Zertifikat am API-Gateway führte zu Verbindungsfehlern aller Clients.",
            ["Zertifikat", "abgelaufen", "TLS", "API-Gateway"], ["Zertifikatserneuerung", "API-Gateway"], ["OPS-3", "OPS-9"], []),
        ("Postmortem 2026-04 Kafka-Lag", "Ein Rebalancing-Sturm im Kafka-Cluster verursachte wachsenden Consumer-Lag im Billing-Service.",
            ["Kafka", "Rebalancing", "Lag"], ["Kafka-Consumer-Lag beheben", "Kafka-Cluster"], ["OPS-4"], []),
        ("Postmortem 2026-05 Datenbank-Failover", "Der automatische Failover der PostgreSQL-Instanz griff erst nach manuellem Eingriff.",
            ["Failover", "PostgreSQL", "Patroni"], ["Datenbank-Failover", "PostgreSQL-Cluster"], ["OPS-5", "OPS-10"], []),
    ]),
    ("Infrastruktur", "Beschreibung der Betriebsplattform im eigenen Rechenzentrum.",
        ["Infrastruktur", "Rechenzentrum", "Plattform"], [], [], [
        ("Kubernetes-Cluster", "Zwei Kubernetes-Cluster (Produktion, Staging) mit Namespaces je Team und GitOps-Deployment.",
            ["Kubernetes", "Namespace", "GitOps", "Helm"], ["CI/CD-Pipeline"], ["OPS-13"], []),
        ("Netzwerk und Firewall", "Netzsegmente, Firewall-Regeln und der Zugriff aus dem Büro- und VPN-Netz.",
            ["Netzwerk", "Firewall", "VPN", "Segment"], ["Zugriffskonzept"], [], []),
        ("PostgreSQL-Cluster", "Hochverfügbarer PostgreSQL-Cluster mit Patroni, synchroner Replikation und täglichen Backups.",
            ["PostgreSQL", "Patroni", "Replikation", "Hochverfügbarkeit"], ["Datenbank-Failover", "Backup und Restore"], ["OPS-5"], []),
        ("Kafka-Cluster", "Kafka-Cluster mit drei Brokern, Topic-Konventionen und Retention-Richtlinien.",
            ["Kafka", "Broker", "Topic", "Retention"], ["Event-Streaming mit Kafka"], ["OPS-4"], []),
        ("Storage", "Persistente Volumes, Objektspeicher für Exporte und Log-Archiv.",
            ["Storage", "Objektspeicher", "Volume", "Archiv"], ["Datenexport", "Speicherplatz bereinigen"], [], []),
    ]),
    ("On-Call-Handbuch", "Regeln für die Rufbereitschaft: Erreichbarkeit, Übergaben und Eskalation.",
        ["On-Call", "Rufbereitschaft", "Eskalation"], ["Runbooks"], [], [
        ("Eskalationsstufen", "Definition der Prioritäten P1 bis P4 mit Reaktionszeiten und Eskalationswegen.",
            ["Eskalation", "Priorität", "Reaktionszeit"], ["Alert-Regeln"], [], []),
        ("Rufbereitschaftsplan", "Wöchentlicher Rotationsplan der Rufbereitschaft und Regeln für Tausch und Vertretung.",
            ["Rotation", "Vertretung", "Bereitschaft"], [], [], []),
    ]),
])

PROD_TREE: P = ("Produkt Startseite", "Produktmanagement: Roadmap, Spezifikationen, Sprint Reviews und Personas.",
    ["Produkt", "Roadmap", "Spezifikation"], [], [], [
    ("Roadmap 2026", "Die Produkt-Roadmap 2026 mit Quartalszielen und priorisierten Initiativen.",
        ["Roadmap", "Ziele", "Priorisierung"], ["Feature-Spezifikationen"], ["ENG-1", "ENG-2", "ENG-3", "ENG-4", "ENG-5"], [
        ("Roadmap Q1 2026", "Schwerpunkt im ersten Quartal: Single Sign-On und Stabilisierung der Anmeldung.",
            ["Q1", "SSO", "Stabilität"], ["SSO-Login"], ["ENG-1"], []),
        ("Roadmap Q2 2026", "Schwerpunkt im zweiten Quartal: bessere Volltextsuche und Datenexport für Mandanten.",
            ["Q2", "Suche", "Export"], ["Volltextsuche", "Datenexport"], ["ENG-2", "ENG-3"], []),
        ("Roadmap Q3 2026", "Schwerpunkt im dritten Quartal: Benachrichtigungscenter und Mandanten-Dashboard.",
            ["Q3", "Benachrichtigungen", "Dashboard"], ["Benachrichtigungscenter", "Mandanten-Dashboard"], ["ENG-4"], []),
        ("Roadmap Q4 2026", "Schwerpunkt im vierten Quartal: Rechnungsportal und Abbau technischer Schulden.",
            ["Q4", "Rechnungsportal", "technische Schulden"], ["Rechnungsportal"], ["ENG-5"], []),
    ]),
    ("Feature-Spezifikationen", "Fachliche Spezifikationen aller größeren Features.",
        ["Spezifikation", "Anforderungen", "Akzeptanzkriterien"], [], [], [
        ("SSO-Login", "Spezifikation für Single Sign-On über den Firmen-Identity-Provider inklusive Mobile-App.",
            ["SSO", "Login", "Identity-Provider", "Mobile"], ["Auth-Service", "ADR-004 OAuth2 mit Keycloak", "Personas"], ["ENG-1", "ENG-6", "ENG-7", "ENG-8", "ENG-10"], []),
        ("Volltextsuche", "Spezifikation der verbesserten Suche: Ranking, Tippfehlertoleranz, Umlaute und Filter.",
            ["Volltextsuche", "Ranking", "Umlaute", "Filter"], ["Search-Service"], ["ENG-2", "ENG-11", "ENG-12", "ENG-13", "ENG-15"], []),
        ("Datenexport", "Spezifikation für Exporte als CSV inklusive asynchroner Erzeugung und Download-Link.",
            ["Export", "CSV", "Download", "asynchron"], ["Reporting-Service", "Löschkonzept"], ["ENG-3", "ENG-16", "ENG-17", "ENG-18", "SEC-5"], []),
        ("Benachrichtigungscenter", "Spezifikation des Benachrichtigungscenters mit Kanälen, Einstellungen und Historie.",
            ["Benachrichtigung", "Kanal", "Einstellungen"], ["Notification-Service"], ["ENG-4", "ENG-21", "ENG-22", "ENG-24", "ENG-25"], []),
        ("Mandanten-Dashboard", "Spezifikation des Dashboards mit Kennzahlen je Mandant und Exportmöglichkeit.",
            ["Dashboard", "Kennzahlen", "Mandant"], ["Reporting-Service", "Datenexport"], [], []),
        ("Rechnungsportal", "Spezifikation des Rechnungsportals: Rechnungsübersicht, PDF-Download, SEPA-Mandat und Archivsuche.",
            ["Rechnung", "PDF", "SEPA", "Archiv"], ["Billing-Service", "Volltextsuche"], ["ENG-5", "ENG-26", "ENG-27", "ENG-29", "ENG-30"], []),
    ]),
    ("Sprint Reviews", "Protokolle der Sprint Reviews mit Ergebnissen und Feedback.",
        ["Sprint Review", "Protokoll", "Feedback"], [], [], [
        (f"Sprint Review {n}", f"Ergebnisse und Feedback aus Sprint {n}.",
            ["Sprint", "Demo", "Feedback"], [], [], []) for n in range(41, 49)
    ]),
    ("Personas", "Typische Nutzergruppen und ihre Ziele.",
        ["Persona", "Nutzer", "Ziele"], [], [], [
        ("Persona Administratorin", "Petra verwaltet Benutzer und Rechte ihres Mandanten und braucht schnelle Exporte.",
            ["Administration", "Rechte", "Export"], ["Datenexport", "Zugriffskonzept"], [], []),
        ("Persona Sachbearbeiter", "Stefan bearbeitet täglich Vorgänge und ist auf eine schnelle, fehlertolerante Suche angewiesen.",
            ["Sachbearbeitung", "Suche", "Effizienz"], ["Volltextsuche"], [], []),
        ("Persona Externer Partner", "Eva greift als externe Partnerin per SSO auf freigegebene Rechnungen zu.",
            ["Partner", "SSO", "Rechnung"], ["SSO-Login", "Rechnungsportal"], [], []),
    ]),
])

SEC_TREE: P = ("Security Startseite", "Informationssicherheit: Richtlinien, Prüfungen, Zugriff und Datenschutz.",
    ["Security", "Informationssicherheit"], [], [], [
    ("Security-Richtlinien", "Verbindliche Sicherheitsrichtlinien für Entwicklung und Betrieb.",
        ["Richtlinie", "Sicherheit", "Pflicht"], ["Code-Review-Richtlinien"], [], [
        ("Passwort- und Token-Richtlinie", "Anforderungen an Passwörter, Personal Access Tokens, Rotation und Speicherung von Secrets.",
            ["Passwort", "Token", "Rotation", "Secrets", "Vault"], ["Auth-Service"], ["SEC-4"], []),
        ("Verschlüsselung", "Vorgaben zur Verschlüsselung im Transit (TLS 1.2+) und at Rest, inklusive interner Zertifizierungsstelle.",
            ["Verschlüsselung", "TLS", "at Rest", "interne CA"], ["Zertifikatserneuerung"], ["SEC-6", "SEC-8"], []),
        ("Secure Coding", "Leitfaden für sicheren Code: Eingabevalidierung, Ausgabe-Encoding, Abhängigkeiten und Secrets.",
            ["Secure Coding", "Validierung", "OWASP", "Abhängigkeiten"], ["Vulnerability-Management"], ["SEC-2", "SEC-3"], []),
    ]),
    ("Penetrationstest 2026", "Ergebnisse des externen Penetrationstests 2026 mit Befunden und Maßnahmenplan.",
        ["Penetrationstest", "Befund", "Maßnahme"], ["Auth-Service", "API-Gateway"], ["SEC-1", "SEC-2", "SEC-8"], []),
    ("Zugriffskonzept", "Rollen, Rechte und Prozesse für die Vergabe und Rezertifizierung von Zugängen.",
        ["Rollen", "Rechte", "Rezertifizierung", "Least Privilege"], ["Auth-Service", "Zugänge beantragen"], ["SEC-7"], []),
    ("Datenschutz (DSGVO)", "Umsetzung der DSGVO: Verarbeitungsverzeichnis, Löschkonzept und Betroffenenrechte.",
        ["DSGVO", "Datenschutz", "personenbezogene Daten"], [], [], [
        ("Verarbeitungsverzeichnis", "Verzeichnis aller Verarbeitungstätigkeiten mit Zweck, Rechtsgrundlage und Löschfristen.",
            ["Verarbeitung", "Rechtsgrundlage", "Löschfrist"], ["Löschkonzept"], [], []),
        ("Löschkonzept", "Regeln und technische Umsetzung für das fristgerechte Löschen personenbezogener Daten, auch in Backups und Exporten.",
            ["Löschung", "Frist", "Backup", "Export"], ["Backup und Restore", "Datenexport"], ["SEC-5"], []),
    ]),
    ("Vulnerability-Management", "Prozess für Erkennung, Bewertung und Behebung von Schwachstellen (CVSS-basiert).",
        ["Schwachstelle", "CVE", "CVSS", "Patch"], ["CI/CD-Pipeline", "Secure Coding"], ["SEC-3"], []),
])

SPACES = [("ENG", ENG_TREE, "Team Plattform"), ("OPS", OPS_TREE, "Team Betrieb"),
          ("PROD", PROD_TREE, "Produktmanagement"), ("SEC", SEC_TREE, "Team Security")]

PEOPLE = ["Anna Becker", "Jonas Weber", "Lea Schmidt", "Mehmet Yilmaz", "Sophie Wagner",
          "Lukas Hoffmann", "Marie Klein", "David Schulz", "Hannah Neumann", "Tobias Krüger"]

OVERVIEW_TEMPLATES = [
    "Diese Seite beschreibt {topic} und richtet sich an alle, die mit {kw0} arbeiten.",
    "Im Mittelpunkt stehen {kw0} und {kw1}; Änderungen werden im zuständigen Team abgestimmt.",
    "Der aktuelle Stand wurde zuletzt im Rahmen von {kw1} überprüft und aktualisiert.",
    "Für {topic} gelten die allgemeinen Vorgaben aus Architektur und Betrieb.",
    "Wichtig ist das Zusammenspiel von {kw0}, {kw1} und {kw2}.",
]
DETAIL_TEMPLATES = [
    "Bei Fragen zu {kw2} hilft ein Blick in die verlinkten Seiten.",
    "Typische Fehlerbilder rund um {kw1} sind im Abschnitt Offene Punkte gesammelt.",
    "Messwerte zu {kw0} werden in den Dashboards des Betriebs ausgewertet.",
    "Konfigurationswerte für {kw2} werden versioniert im Repository gepflegt.",
    "Abweichungen von diesem Vorgehen müssen dokumentiert und begründet werden.",
]
TODO_TEMPLATES = [
    "- {kw0} für weitere Mandanten ausrollen",
    "- Dokumentation zu {kw1} ergänzen",
    "- Monitoring für {kw2} verbessern",
    "- Verantwortlichkeiten für {kw0} klären",
    "- Lasttest mit realistischen Daten für {kw1} wiederholen",
]

# --------------------------------------------------------------------------- jira

EPICS = [
    ("ENG-1", "Single Sign-On einführen", "SSO-Login", [
        "Keycloak-Realm für Mandanten konfigurieren", "Login-Seite auf OpenID Connect umstellen",
        "Token-Refresh im Web-Client implementieren", "Bug: Logout beendet Keycloak-Session nicht",
        "SSO für Mobile-App mit PKCE"]),
    ("ENG-2", "Volltextsuche verbessern", "Volltextsuche", [
        "Relevanz-Ranking mit Titelgewichtung", "Tippfehlertoleranz für Suchbegriffe",
        "Bug: Umlaute werden in der Suche nicht gefunden", "Suchindex inkrementell aktualisieren",
        "Facettenfilter nach Projekt und Typ"]),
    ("ENG-3", "Datenexport für Mandanten", "Datenexport", [
        "CSV-Export für Berichte", "Export als asynchroner Job über Kafka",
        "Bug: Export bricht bei mehr als 10.000 Zeilen ab", "Download-Link für Exporte per E-Mail",
        "Berechtigungsprüfung für Exporte"]),
    ("ENG-4", "Benachrichtigungscenter", "Benachrichtigungscenter", [
        "Benachrichtigungen als Kafka-Events veröffentlichen", "E-Mail-Templates überarbeiten",
        "Push-Benachrichtigungen für Android", "Bug: Doppelte Benachrichtigungen nach Retry",
        "Einstellungen für Benachrichtigungskanäle"]),
    ("ENG-5", "Rechnungsportal", "Rechnungsportal", [
        "SEPA-Lastschrift anbinden", "Rechnungs-PDF erzeugen", "Bug: Rundungsfehler bei Teilbeträgen",
        "Zahlungsstatus-Webhook verarbeiten", "Rechnungsarchiv mit Volltextsuche"]),
]

OPS_ISSUES = [  # key, type, summary, status, confluence titles, related jira
    ("OPS-1", "Incident", "Login nicht möglich – Keycloak antwortet mit 502", "Erledigt",
     ["Postmortem 2026-01 Login-Ausfall", "Deployment-Rollback"], ["ENG-7"]),
    ("OPS-2", "Incident", "Zahlungseingänge werden verzögert verbucht", "Erledigt",
     ["Postmortem 2026-02 Zahlungsverzögerung"], ["OPS-4", "ENG-29"]),
    ("OPS-3", "Incident", "TLS-Zertifikat für api.firma.intern abgelaufen", "Erledigt",
     ["Postmortem 2026-03 Zertifikat abgelaufen", "Zertifikatserneuerung"], ["OPS-9", "SEC-8"]),
    ("OPS-4", "Incident", "Kafka-Consumer-Lag im Billing-Service", "Erledigt",
     ["Postmortem 2026-04 Kafka-Lag", "Kafka-Consumer-Lag beheben"], ["OPS-11"]),
    ("OPS-5", "Incident", "Primäre PostgreSQL-Instanz nicht erreichbar", "Erledigt",
     ["Postmortem 2026-05 Datenbank-Failover", "Datenbank-Failover"], ["OPS-10"]),
    ("OPS-6", "Incident", "Festplatte auf dem Log-Server voll", "Erledigt",
     ["Speicherplatz bereinigen"], []),
    ("OPS-7", "Incident", "Erhöhte Latenz im API-Gateway bei Lastspitzen", "In Arbeit",
     ["Skalierung bei Lastspitzen", "API-Gateway"], ["OPS-12"]),
    ("OPS-8", "Incident", "Backup-Job schlägt sporadisch fehl", "In Arbeit",
     ["Backup und Restore"], ["SEC-6", "OPS-14"]),
    ("OPS-9", "Task", "Zertifikatserneuerung automatisieren", "In Arbeit",
     ["Zertifikatserneuerung"], ["SEC-8"]),
    ("OPS-10", "Task", "Failover-Übung quartalsweise durchführen", "Offen",
     ["Datenbank-Failover"], []),
    ("OPS-11", "Task", "Alert für Kafka-Consumer-Lag einführen", "Erledigt",
     ["Alert-Regeln", "Kafka-Consumer-Lag beheben"], []),
    ("OPS-12", "Task", "Grafana-Dashboard für SLOs erstellen", "In Review",
     ["Grafana-Dashboards", "SLOs und Fehlerbudgets"], []),
    ("OPS-13", "Task", "Kubernetes auf neue Minor-Version aktualisieren", "Offen",
     ["Kubernetes-Cluster"], []),
    ("OPS-14", "Task", "Restore-Test für PostgreSQL dokumentieren", "Offen",
     ["Backup und Restore", "PostgreSQL-Cluster"], []),
]

SEC_ISSUES = [
    ("SEC-1", "Schwachstelle", "Pentest-Befund: fehlende Rate Limits am Login", "In Arbeit",
     ["Penetrationstest 2026", "Auth-Service"], ["ENG-1"]),
    ("SEC-2", "Schwachstelle", "Pentest-Befund: Session-Cookie ohne SameSite-Attribut", "Erledigt",
     ["Penetrationstest 2026", "Secure Coding"], ["ENG-8"]),
    ("SEC-3", "Schwachstelle", "Abhängigkeit mit kritischer CVE aktualisieren", "Erledigt",
     ["Vulnerability-Management", "CI/CD-Pipeline"], []),
    ("SEC-4", "Task", "Secrets aus CI-Variablen in Vault migrieren", "In Arbeit",
     ["Passwort- und Token-Richtlinie", "CI/CD-Pipeline"], []),
    ("SEC-5", "Task", "Löschkonzept für Exportdateien umsetzen", "Offen",
     ["Löschkonzept", "Datenexport"], ["ENG-3"]),
    ("SEC-6", "Task", "Verschlüsselung der Backups prüfen", "In Review",
     ["Verschlüsselung", "Backup und Restore"], []),
    ("SEC-7", "Task", "Zugriffsrechte jährlich rezertifizieren", "Offen",
     ["Zugriffskonzept"], []),
    ("SEC-8", "Task", "TLS 1.0 und 1.1 am API-Gateway deaktivieren", "Erledigt",
     ["Verschlüsselung", "API-Gateway"], []),
]

STATUSES = ["Offen", "In Arbeit", "In Review", "Erledigt"]
ISSUE_LINK_TYPES = [("blocks", "is blocked by"), ("relates to", "relates to"),
                    ("duplicates", "is duplicated by"), ("causes", "is caused by")]


@dataclass
class _Page:
    id: str
    space: str
    team: str
    title: str
    desc: str
    kws: list[str]
    link_titles: list[str]
    jira: list[str]
    parent: "_Page | None" = None
    children: list["_Page"] = field(default_factory=list)


@dataclass
class _Issue:
    key: str
    type: str
    summary: str
    status: str
    description: list[tuple[str, str]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    links: list[LinkTarget] = field(default_factory=list)


def _flatten(space: str, team: str, node: tuple, parent: _Page | None, out: list[_Page]) -> None:
    title, desc, kws, links, jira, children = node
    page = _Page(id=str(FIRST_PAGE_ID + len(out)), space=space, team=team, title=title, desc=desc,
                 kws=list(kws), link_titles=list(links), jira=list(jira), parent=parent)
    out.append(page)
    if parent:
        parent.children.append(page)
    for child in children:
        _flatten(space, team, child, page, out)


def _date(rng: random.Random) -> str:
    start = datetime(2025, 9, 1, tzinfo=timezone.utc)
    return (start + timedelta(days=rng.randint(0, 400), hours=rng.randint(7, 18),
                              minutes=rng.randint(0, 59))).isoformat(timespec="seconds")


def _issue_dates(rng: random.Random) -> tuple[str, str]:
    start = datetime(2025, 9, 1, tzinfo=timezone.utc)
    created_dt = start + timedelta(
        days=rng.randint(0, 300),
        hours=rng.randint(7, 18),
        minutes=rng.randint(0, 59),
    )
    updated_dt = created_dt + timedelta(
        days=rng.randint(1, 90),
        hours=rng.randint(1, 12),
        minutes=rng.randint(0, 59),
    )
    return (
        created_dt.isoformat(timespec="seconds"),
        updated_dt.isoformat(timespec="seconds"),
    )


def _fill(template: str, topic: str, kws: list[str]) -> str:
    k = (kws * 3)[:3]
    return template.format(topic=topic, kw0=k[0], kw1=k[1], kw2=k[2])


def build_demo(ctx: LinkContext) -> tuple[list[FetchedDocument], dict[str, str]]:
    """Return (documents, special_states uid->state) in a deterministic order."""
    rng = random.Random(20261010)
    pages: list[_Page] = []
    for space, tree, team in SPACES:
        _flatten(space, team, tree, None, pages)
    by_title = {p.title: p for p in pages}

    # ---------------------------------------------------------------- issues
    issues: dict[str, _Issue] = {}
    # Stories get ENG-6..ENG-30 (five per epic), sub-tasks are numbered afterwards.
    next_eng = 6
    epic_children: dict[str, list[str]] = {}
    for epic_key, epic_summary, _spec, stories in EPICS:
        issues[epic_key] = _Issue(epic_key, "Epic", epic_summary, rng.choice(["In Arbeit", "Offen", "In Arbeit"]),
                                  labels=["roadmap-2026"])
        epic_children[epic_key] = []
        for summary in stories:
            key = f"ENG-{next_eng}"
            next_eng += 1
            itype = "Bug" if summary.startswith("Bug:") else "Story"
            labels = (["bug"] if itype == "Bug" else []) + [epic_summary.split()[0].lower()]
            issues[key] = _Issue(key, itype, summary, rng.choice(STATUSES), labels=labels)
            epic_children[epic_key].append(key)
    for epic_key, _summary, _spec, _stories in EPICS:
        for key in epic_children[epic_key][:2]:  # two sub-tasks for the first two stories
            summary = issues[key].summary
            for sub_summary in (f"Umsetzung: {summary}", f"Tests und Doku: {summary}"):
                skey = f"ENG-{next_eng}"
                next_eng += 1
                issues[skey] = _Issue(skey, "Sub-task", sub_summary, rng.choice(STATUSES))
                issues[key].links.append(LinkTarget(jira_uid(skey), EDGE_JIRA_PARENT_OF, "parent of", "subtasks",
                                                    skey, jira_issue_url(ctx, skey), f"{skey}: {sub_summary}"))
                issues[skey].links.append(LinkTarget(jira_uid(key), EDGE_JIRA_PARENT_OF, "child of", "parent",
                                                     key, jira_issue_url(ctx, key), f"{key}: {summary}"))
    for key, itype, summary, status, _conf, _rel in OPS_ISSUES + SEC_ISSUES:
        issues[key] = _Issue(key, itype, summary, status, labels=[itype.lower()])

    # epic <-> story hierarchy and Confluence back-links
    for epic_key, _summary, spec_title, _stories in EPICS:
        epic = issues[epic_key]
        spec = by_title[spec_title]
        epic.links.append(_conf_link(ctx, spec, "Spezifikation"))
        for child in epic_children[epic_key]:
            epic.links.append(LinkTarget(jira_uid(child), EDGE_JIRA_PARENT_OF, "parent of", "epic",
                                         child, jira_issue_url(ctx, child), f"{child}: {issues[child].summary}"))
            issues[child].links.append(LinkTarget(jira_uid(epic_key), EDGE_JIRA_PARENT_OF, "child of", "parent",
                                                  epic_key, jira_issue_url(ctx, epic_key), f"{epic_key}: {epic.summary}"))
        # a few explicit relations between stories of the same epic
        kids = epic_children[epic_key]
        a, b = kids[0], kids[1]
        _relate(ctx, issues, b, a, "blocks", "is blocked by")
        _relate(ctx, issues, kids[3], kids[2], "relates to", "relates to")
    for key, _t, _s, _st, conf_titles, related in OPS_ISSUES + SEC_ISSUES:
        for title in conf_titles:
            issues[key].links.append(_conf_link(ctx, by_title[title], "Runbook" if "Postmortem" not in title else "Postmortem"))
        for rel in related:
            if rel in issues:
                _relate(ctx, issues, key, rel, "relates to", "relates to")
    # Deterministic cross-project links and one duplicate
    _relate(ctx, issues, "ENG-7", "ENG-9", "causes", "is caused by")
    _relate(ctx, issues, "ENG-24", "ENG-21", "is caused by", "causes")
    _relate(ctx, issues, "ENG-28", "ENG-29", "relates to", "relates to")
    _relate(ctx, issues, "ENG-30", "ENG-13", "duplicates", "is duplicated by")
    issues["ENG-7"].links.append(_conf_link(ctx, by_title["Postmortem 2026-01 Login-Ausfall"], "Postmortem"))
    # Not indexed / inaccessible references
    issues["ENG-9"].links.append(LinkTarget(jira_uid("LEGACY-17"), EDGE_JIRA_RELATES_TO, "relates to", "issuelinks",
                                            "LEGACY-17", jira_issue_url(ctx, "LEGACY-17"), "LEGACY-17"))
    issues["OPS-6"].links.append(LinkTarget(jira_uid("HR-5"), EDGE_JIRA_RELATES_TO, "relates to", "issuelinks",
                                            "HR-5", jira_issue_url(ctx, "HR-5"), "HR-5"))

    # descriptions
    for issue in issues.values():
        issue.description = _issue_description(issue, rng)

    # ---------------------------------------------------------------- sprint reviews mention stories
    story_keys = [k for k, i in issues.items() if i.type in ("Story", "Bug")]
    for page in pages:
        if page.title.startswith("Sprint Review "):
            page.jira = sorted(rng.sample(story_keys, 4), key=lambda k: int(k.split("-")[1]))
            page.link_titles = ["Roadmap 2026", rng.choice(["SSO-Login", "Volltextsuche", "Datenexport", "Rechnungsportal"])]
            page.kws = ["Sprint", "Demo", issues[page.jira[0]].summary.replace("Bug: ", "")]

    # ---------------------------------------------------------------- documents
    docs: list[FetchedDocument] = []
    for page in pages:
        docs.append(_page_document(ctx, page, by_title, issues, rng))
    for issue in sorted(issues.values(), key=lambda i: (i.key.split("-")[0], int(i.key.split("-")[1]))):
        docs.append(_issue_document(ctx, issue, rng))

    # Extra reference/state examples
    special = {
        confluence_uid("999001"): STATE_MISSING,
        jira_uid("HR-5"): STATE_FORBIDDEN,
    }
    return docs, special


def _conf_link(ctx: LinkContext, page: _Page, label: str) -> LinkTarget:
    return LinkTarget(confluence_uid(page.id), EDGE_LINKS_TO_CONFLUENCE, label.lower(), "remotelink",
                      page.title, confluence_page_url(ctx, page.id), page.title)


def _relate(ctx: LinkContext, issues: dict[str, _Issue], a: str, b: str, outward: str, inward: str) -> None:
    ia, ib = issues[a], issues[b]
    ia.links.append(LinkTarget(jira_uid(b), EDGE_JIRA_RELATES_TO, outward, "issuelinks", b,
                               jira_issue_url(ctx, b), f"{b}: {ib.summary}"))
    ib.links.append(LinkTarget(jira_uid(a), EDGE_JIRA_RELATES_TO, inward, "issuelinks", a,
                               jira_issue_url(ctx, a), f"{a}: {ia.summary}"))


def _issue_description(issue: _Issue, rng: random.Random) -> list[tuple[str, str]]:
    s = issue.summary.replace("Bug: ", "")
    if issue.type == "Bug":
        return [("Fehlerbild", f"{s}. Der Fehler tritt reproduzierbar in Staging und Produktion auf."),
                ("Schritte zur Reproduktion", "1. Anmelden\n2. Funktion aufrufen\n3. Ergebnis prüfen"),
                ("Erwartetes Verhalten", "Die Funktion arbeitet korrekt und ohne Fehlermeldung.")]
    if issue.type == "Incident":
        return [("Auswirkung", f"{s}. Betroffen waren mehrere Mandanten; der Betrieb wurde per Alert informiert."),
                ("Zeitlicher Ablauf", f"Erkannt um {rng.randint(1, 23):02d}:{rng.randint(0, 59):02d} Uhr, "
                                      f"behoben nach {rng.randint(15, 240)} Minuten."),
                ("Maßnahmen", "Ursache analysiert, Runbook angewendet, Postmortem erstellt.")]
    if issue.type == "Schwachstelle":
        return [("Befund", f"{s}. Bewertung nach CVSS: {rng.choice(['5.3', '6.5', '7.5', '8.1'])}."),
                ("Empfehlung", "Befund priorisiert beheben und Behebung durch Retest bestätigen.")]
    if issue.type == "Epic":
        return [("Ziel", f"{s} als zusammenhängende Initiative der Roadmap 2026."),
                ("Umfang", "Die zugehörigen Stories sind im Epic verlinkt; Details stehen in der Spezifikation.")]
    return [("Beschreibung", f"Als Nutzer möchte ich: {s}."),
            ("Akzeptanzkriterien", f"- {s} ist umgesetzt\n- Tests sind grün\n- Dokumentation ist aktualisiert")]


def _page_document(ctx: LinkContext, page: _Page, by_title: dict[str, _Page],
                   issues: dict[str, _Issue], rng: random.Random) -> FetchedDocument:
    links: list[LinkTarget] = []
    for child in page.children:
        links.append(LinkTarget(confluence_uid(child.id), EDGE_PARENT_OF, "parent of", "children",
                                child.title, confluence_page_url(ctx, child.id), child.title))
    mentions_text: list[str] = []
    for title in page.link_titles:
        target = by_title.get(title)
        if target and target is not page:
            links.append(LinkTarget(confluence_uid(target.id), EDGE_LINKS_TO, "", "ac:link",
                                    target.title, "", target.title))
            mentions_text.append(f"Siehe auch: {target.title}.")
    for key in page.jira:
        issue = issues.get(key)
        if issue:
            links.append(LinkTarget(jira_uid(key), EDGE_MENTIONS_JIRA, "", "jira-macro", key,
                                    jira_issue_url(ctx, key), f"{key}: {issue.summary}"))
            mentions_text.append(f"Umsetzung in {key} ({issue.summary}, Status {issue.status}).")
    # References that are deliberately not resolvable / not indexed
    if page.title == "Erste Woche":
        links.append(LinkTarget(unresolved_page_uid("HR", "Urlaubsregelung"), EDGE_LINKS_TO, "", "ac:link",
                                "Urlaubsregelung", "", "Urlaubsregelung"))
        mentions_text.append("Urlaubsfragen klärt die Seite Urlaubsregelung im HR-Space.")
    if page.title == "Glossar":
        links.append(LinkTarget(confluence_uid("999001"), EDGE_LINKS_TO, "", "body", "Altes Glossar (gelöscht)",
                                confluence_page_url(ctx, "999001"), "Altes Glossar"))
    if page.title == "Datenbank-Failover":
        links.append(LinkTarget(jira_uid("LEGACY-17"), EDGE_MENTIONS_JIRA, "", "jira-macro", "LEGACY-17",
                                jira_issue_url(ctx, "LEGACY-17"), "LEGACY-17"))

    owner = rng.choice(PEOPLE)
    overview = " ".join(_fill(t, page.title, page.kws) for t in rng.sample(OVERVIEW_TEMPLATES, 3))
    details = " ".join(_fill(t, page.title, page.kws) for t in rng.sample(DETAIL_TEMPLATES, 2))
    todos = "\n".join(_fill(t, page.title, page.kws) for t in rng.sample(TODO_TEMPLATES, 2))
    sections = [
        ("", page.desc),
        ("Überblick", overview),
        ("Details", details + ("\n" + "\n".join(mentions_text) if mentions_text else "")),
        ("Verantwortlichkeiten", f"Verantwortlich ist {page.team}. Ansprechperson: {owner}."),
        ("Offene Punkte", todos),
    ]
    if page.children:
        sections.insert(2, ("Unterseiten", "\n".join(f"- {c.title}" for c in page.children)))
    body = "\n\n".join(f"{h}\n{t}" if h else t for h, t in sections)
    return FetchedDocument(
        uid=confluence_uid(page.id), source_type="confluence", source_id=page.id, title=page.title,
        url=confluence_page_url(ctx, page.id), body_text=body, sections=sections, links=links,
        space_or_project=page.space, document_type="page", source_version=str(rng.randint(1, 40)),
        source_updated_at=_date(rng), labels=[k.lower().replace(" ", "-") for k in page.kws[:2]])


def _issue_document(ctx: LinkContext, issue: _Issue, rng: random.Random) -> FetchedDocument:
    header = [f"{issue.key}: {issue.summary}", f"Status: {issue.status}", f"Typ: {issue.type}"]
    if issue.labels:
        header.append("Labels: " + ", ".join(issue.labels))
    sections = [("", "\n".join(header))] + issue.description
    body = "\n\n".join(f"{h}\n{t}" if h else t for h, t in sections)
    created_at, updated_at = _issue_dates(rng)
    return FetchedDocument(
        uid=jira_uid(issue.key), source_type="jira", source_id=issue.key,
        title=f"{issue.key}: {issue.summary}", url=jira_issue_url(ctx, issue.key), body_text=body,
        sections=sections, links=issue.links, space_or_project=issue.key.split("-")[0],
        status_label=issue.status, document_type=issue.type,
        source_created_at=created_at, source_updated_at=updated_at,
        labels=issue.labels)


def reset_database(conn: sqlite3.Connection) -> None:
    """Delete all documents, edges, chunks and jobs."""
    with db.transaction(conn):
        conn.execute("DELETE FROM crawl_jobs")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM documents")


def seed_demo(conn: sqlite3.Connection, cfg: dict, *, reset: bool = False) -> dict:
    """Write the demo data set. Idempotent: re-running updates the same UIDs."""
    demo_cfg = {
        "confluence": {"base_url": cfg["confluence"].get("base_url") or DEMO_CONFLUENCE_BASE, "aliases": []},
        "jira": {"base_url": cfg["jira"].get("base_url") or DEMO_JIRA_BASE, "aliases": []},
    }
    ctx = LinkContext.from_config(demo_cfg)
    if reset:
        reset_database(conn)
    docs, special = build_demo(ctx)
    idx = cfg.get("index", {})
    pages = issues = 0
    for doc in docs:
        chunks = chunk_sections(doc.title, doc.sections,
                                target_chars=int(idx.get("chunk_target_chars", 1200)),
                                overlap_chars=int(idx.get("chunk_overlap_chars", 150)),
                                min_chars=int(idx.get("min_chunk_chars", 80)))
        db.save_document(conn, doc, chunks)
        if doc.source_type == "confluence":
            pages += 1
        else:
            issues += 1
    for uid, st in special.items():
        msg = "Seite existiert nicht mehr (HTTP 404)" if st == STATE_MISSING else "Zugriff verweigert (HTTP 403)"
        db.mark_document_state(conn, uid, st, msg)

    start_uid = confluence_uid(str(FIRST_PAGE_ID))
    job_id = db.create_job(conn, {"demo": True, "start_page": start_uid}, start_uid)
    db.update_job(conn, job_id, state="completed", finished_at=db.now_iso(), pages_indexed=pages,
                  jira_indexed=issues, api_requests=0,
                  last_message=f"Demo-Daten erzeugt: {pages} Seiten, {issues} Jira-Tickets")
    db.add_job_log(conn, job_id, "info", "Demo-Daten ohne Atlassian-Zugriff erzeugt")
    return {"pages": pages, "issues": issues, "job_id": job_id,
            "confluence_base": demo_cfg["confluence"]["base_url"], "jira_base": demo_cfg["jira"]["base_url"]}
