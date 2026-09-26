"""One-off generator for the sample research deliverable shown in the Inbox.

Writes app/static/deliverables/northwind-equity-research.pdf. The company and
every figure are fictional. reportlab is not an app dependency; run this with
any Python that has it installed:

    python -m venv /tmp/pdfvenv && /tmp/pdfvenv/bin/pip install reportlab
    /tmp/pdfvenv/bin/python scripts/make_sample_deliverable.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

OUT = Path(__file__).resolve().parent.parent / "app/static/deliverables/northwind-equity-research.pdf"

INK = colors.HexColor("#0F172A")
INK2 = colors.HexColor("#475569")
LINE = colors.HexColor("#E2E8F0")
ACCENT = colors.HexColor("#0E7C7B")
SOFT = colors.HexColor("#F1F5F9")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=20,
                    leading=24, textColor=INK, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=13.5,
                    leading=17, textColor=ACCENT, spaceBefore=14, spaceAfter=6)
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontName="Helvetica-Bold", fontSize=10.5,
                    leading=14, textColor=INK, spaceBefore=8, spaceAfter=3)
BODY = ParagraphStyle("Body", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.8,
                      leading=14.2, textColor=INK, alignment=TA_LEFT, spaceAfter=6)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8.2, leading=11, textColor=INK2)
BULLET = ParagraphStyle("Bullet", parent=BODY, leftIndent=12, bulletIndent=2, spaceAfter=3)
KICK = ParagraphStyle("Kick", parent=BODY, fontName="Helvetica-Bold", fontSize=8.5,
                      textColor=ACCENT, spaceAfter=2)


def p(text, style=BODY):
    return Paragraph(text, style)


def bullets(items):
    return [Paragraph(t, BULLET, bulletText="•") for t in items]


def table(rows, widths, *, num_cols=(), bold_last=False):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.6),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.8),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK2),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for c in num_cols:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if bold_last:
        style += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.8),
                  ("BACKGROUND", (0, -1), (-1, -1), SOFT)]
    t.setStyle(TableStyle(style))
    return t


def on_page(canvas, doc):
    canvas.saveState()
    w, h = LETTER
    canvas.setFillColor(ACCENT)
    canvas.rect(0, h - 6, w, 6, stroke=0, fill=1)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(INK)
    canvas.drawString(0.8 * inch, h - 0.5 * inch, "LEDGERLINE RESEARCH")
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(INK2)
    canvas.drawRightString(w - 0.8 * inch, h - 0.5 * inch,
                           "Northwind Robotics, Inc. (NWRB)  |  Initiation of coverage")
    canvas.setStrokeColor(LINE)
    canvas.line(0.8 * inch, 0.7 * inch, w - 0.8 * inch, 0.7 * inch)
    canvas.drawString(0.8 * inch, 0.5 * inch,
                      "Fictional company. All figures are illustrative and for demonstration only.")
    canvas.drawRightString(w - 0.8 * inch, 0.5 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build():
    story = []

    # ── cover / executive summary ────────────────────────────────────────────
    story += [p("EQUITY RESEARCH  |  INDUSTRIAL AUTOMATION", KICK),
              p("Northwind Robotics, Inc. (NWRB)", H1),
              p("Initiation of coverage: warehouse autonomy at an inflection point",
                ParagraphStyle("Sub", parent=BODY, fontSize=12, leading=16, textColor=INK2)),
              Spacer(1, 10)]
    kpi = Table([
        ["Rating", "Price target", "Last price", "Upside", "Market cap"],
        ["Outperform", "$58.00", "$47.20", "+22.9%", "$6.1B"],
    ], colWidths=[1.3 * inch] * 5, hAlign="LEFT")
    kpi.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica", 8), ("TEXTCOLOR", (0, 0), (-1, 0), INK2),
        ("FONT", (0, 1), (-1, 1), "Helvetica-Bold", 14), ("TEXTCOLOR", (0, 1), (-1, 1), INK),
        ("TEXTCOLOR", (0, 1), (0, 1), ACCENT),
        ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [kpi, Spacer(1, 6), p("Report date: 27 September 2026. Prepared by Ledgerline "
                                   "Financial Analyst for the commissioning client.", SMALL)]
    story += [p("Executive summary", H2)]
    story += [p("Northwind Robotics designs autonomous mobile robots (AMRs) and the fleet software "
                "that coordinates them inside warehouses and distribution centres. FY2025 revenue "
                "grew 24% to $412M, and the mix shift toward recurring software lifted gross margin "
                "to 47.8%. We initiate at <b>Outperform</b> with a <b>$58 price target</b>, set at "
                "the midpoint of our discounted cash flow value ($58.40) and a comparables range "
                "of $52 to $61.")]
    story += bullets([
        "<b>Recurring revenue is compounding.</b> Fleet software and service subscriptions reached "
        "31% of revenue (FY2023: 22%) with 118% net revenue retention.",
        "<b>Operating leverage is visible.</b> Operating margin expanded 310 bps to 9.6% as R&amp;D "
        "grew slower than revenue for the second consecutive year.",
        "<b>Balance sheet is clean.</b> $540M net cash funds the roadmap with no dilution in our base case.",
        "<b>Key risks:</b> customer concentration (top five: 38% of revenue), competitive pricing "
        "from larger automation vendors, and execution on the European expansion.",
    ])
    story += [p("Investment thesis", H3),
              p("The warehouse automation market remains under-penetrated: we estimate fewer than "
                "one in five large distribution centres run autonomous material handling today. "
                "Northwind's interoperable fleet software, which coordinates its own robots and "
                "third-party equipment, gives it a wedge into brownfield sites that competitors "
                "with closed ecosystems struggle to serve. We expect subscription revenue to reach "
                "40% of the mix by FY2028, supporting a structurally higher margin profile.")]
    story.append(PageBreak())

    # ── business overview ────────────────────────────────────────────────────
    story += [p("Business overview", H2),
              p("Founded in 2014 and headquartered in Columbus, Ohio, Northwind sells into "
                "third-party logistics providers, e-commerce retailers and grocery distribution. "
                "The company reports two segments:"),
              ]
    story += bullets([
        "<b>Systems (69% of FY2025 revenue).</b> AMR hardware (the Tern and Heron platforms), "
        "installation and site integration. Sold per robot with a typical site deploying 40 to 120 units.",
        "<b>Software and Services (31%).</b> Fleet orchestration software, analytics and "
        "maintenance contracts billed annually per robot under management.",
    ])
    story += [p("Customers and go to market", H3),
              p("Northwind serves 214 customers across 1,050 sites. Direct sales cover North "
                "America; a channel partnership launched in FY2025 opens Germany, the Netherlands "
                "and the Nordics. Average contract length for software is 3.4 years, and 92% of "
                "robots shipped since FY2022 remain under an active software subscription."),
              p("Competitive position", H3),
              p("The competitive set includes large diversified automation vendors and a number "
                "of venture-backed AMR specialists. Northwind differentiates on interoperability "
                "(its orchestration layer supports more than 30 third-party device types) and on "
                "deployment speed: the median site goes live in 11 weeks versus an industry "
                "median we estimate at 16 to 20 weeks."),
              p("Management and governance", H3),
              p("The CEO and CFO have led the company since before its 2021 listing. Board "
                "composition is majority independent, and executive incentives are tied to "
                "subscription revenue growth and free cash flow, which we view as well aligned "
                "with long-term shareholder value.")]
    seg = [["Segment revenue ($M)", "FY2023", "FY2024", "FY2025", "CAGR"],
           ["Systems", "209.0", "243.4", "284.3", "16.6%"],
           ["Software and Services", "59.0", "88.6", "127.7", "47.1%"],
           ["Total", "268.0", "332.0", "412.0", "24.0%"]]
    story += [Spacer(1, 6), table(seg, [2.4 * inch, 1 * inch, 1 * inch, 1 * inch, 0.9 * inch],
                                  num_cols=(1, 2, 3, 4), bold_last=True)]
    story.append(PageBreak())

    # ── financial analysis ───────────────────────────────────────────────────
    story += [p("Financial analysis", H2),
              p("Revenue growth has been consistent and increasingly driven by software. Gross "
                "margin expanded 520 bps over two years as software rose in the mix and hardware "
                "unit costs fell with the Heron platform redesign. Operating expenses grew at "
                "roughly two thirds the rate of revenue.")]
    fin = [["Income statement ($M)", "FY2023", "FY2024", "FY2025"],
           ["Revenue", "268.0", "332.0", "412.0"],
           ["  growth", "19.1%", "23.9%", "24.1%"],
           ["Gross profit", "114.2", "150.7", "196.9"],
           ["  gross margin", "42.6%", "45.4%", "47.8%"],
           ["Operating expenses", "96.8", "129.2", "157.4"],
           ["Operating income", "17.4", "21.5", "39.5"],
           ["  operating margin", "6.5%", "6.5%", "9.6%"],
           ["Net income", "13.9", "18.8", "33.1"],
           ["Diluted EPS ($)", "0.11", "0.15", "0.26"],
           ["Free cash flow", "8.1", "22.6", "41.3"]]
    story += [table(fin, [2.6 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch], num_cols=(1, 2, 3))]
    story += [p("Cash flow and balance sheet", H3),
              p("Free cash flow turned consistently positive in FY2024 and reached $41.3M in "
                "FY2025 (10.0% margin). Working capital intensity fell as deployment times shortened. "
                "Northwind ends FY2025 with $540M of cash and investments and no funded debt."),
              p("Forecast", H3),
              p("We model revenue of $498M in FY2026 and $594M in FY2027 (21% and 19% growth), "
                "with gross margin reaching 50.5% by FY2027 and operating margin of 13.2%. Our "
                "estimates assume European revenue contributes 6% of the total by FY2027.")]
    est = [["Estimates ($M)", "FY2026E", "FY2027E", "FY2028E"],
           ["Revenue", "498.5", "593.2", "694.0"],
           ["Gross margin", "49.2%", "50.5%", "51.6%"],
           ["Operating margin", "11.4%", "13.2%", "14.8%"],
           ["Free cash flow", "54.8", "72.9", "93.4"]]
    story += [table(est, [2.6 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch], num_cols=(1, 2, 3))]
    story.append(PageBreak())

    # ── valuation ────────────────────────────────────────────────────────────
    story += [p("Valuation", H2),
              p("Our $58 target weights a ten-year discounted cash flow and a peer multiples "
                "cross-check equally, rounded to the nearest dollar."),
              p("Discounted cash flow", H3)]
    dcf = [["DCF assumptions and output", "Value"],
           ["Weighted average cost of capital", "9.4%"],
           ["Terminal growth rate", "3.0%"],
           ["Explicit forecast period", "FY2026 to FY2035"],
           ["PV of forecast free cash flow", "$1,742M"],
           ["PV of terminal value", "$4,810M"],
           ["Enterprise value", "$6,552M"],
           ["Plus net cash", "$540M"],
           ["Equity value", "$7,092M"],
           ["Diluted shares", "121.4M"],
           ["Value per share", "$58.40"]]
    story += [table(dcf, [3.4 * inch, 1.5 * inch], num_cols=(1,), bold_last=True)]
    sens = [["WACC \\ g", "2.5%", "3.0%", "3.5%"],
            ["8.9%", "$60.10", "$63.20", "$66.90"],
            ["9.4%", "$55.70", "$58.40", "$61.50"],
            ["9.9%", "$51.90", "$54.30", "$57.00"]]
    story += [p("Sensitivity of value per share", H3),
              table(sens, [1.2 * inch, 1 * inch, 1 * inch, 1 * inch], num_cols=(1, 2, 3))]
    story += [p("Comparable companies", H3)]
    comps = [["Company (fictional)", "EV / Sales FY26E", "EV / EBIT FY26E", "Rev growth"],
             ["Harbor Automation", "5.8x", "41.2x", "17%"],
             ["Meridian Logistics Tech", "6.9x", "48.0x", "22%"],
             ["Osprey Industrial", "3.1x", "22.5x", "8%"],
             ["Quayside Systems", "7.4x", "55.3x", "26%"],
             ["Talon Motion", "4.6x", "35.9x", "14%"],
             ["Westbrook Controls", "3.8x", "27.4x", "9%"],
             ["Peer median", "5.2x", "38.6x", "15.5%"],
             ["Northwind at $58 target", "13.1x", "114.7x", "21%"]]
    story += [table(comps, [2.4 * inch, 1.3 * inch, 1.3 * inch, 1 * inch], num_cols=(1, 2, 3),
                    bold_last=True),
              Spacer(1, 4),
              p("Northwind trades at a premium to the peer median on current earnings, which we "
                "view as justified by faster growth and a higher recurring mix. Applying growth "
                "adjusted EV / Sales multiples of 11.5x to 13.5x to FY2026E revenue yields $52 to "
                "$61 per share.", BODY)]
    story.append(PageBreak())

    # ── risks and recommendation ─────────────────────────────────────────────
    story += [p("Key risks", H2)]
    risks = [
        ("Customer concentration", "The top five customers account for 38% of revenue. The loss "
         "or delay of a single large programme could reduce FY2026 revenue by 5% to 8%."),
        ("Competitive pricing", "Larger automation vendors may bundle AMRs at low margin to win "
         "whole-facility contracts, pressuring Systems pricing."),
        ("International execution", "The European channel is new; slower partner ramp would push "
         "out our FY2027 international contribution."),
        ("Supply chain", "Lidar sensors and drive units come from a small set of suppliers; "
         "shortages could delay deliveries and revenue recognition."),
        ("Macro sensitivity", "Warehouse capital budgets follow e-commerce volumes and interest "
         "rates. A sharp slowdown would lengthen sales cycles."),
    ]
    for title, text in risks:
        story.append(KeepTogether([p(title, H3), p(text)]))
    story += [p("Recommendation", H2),
              p("We initiate coverage of Northwind Robotics with an <b>Outperform</b> rating and a "
                "12-month price target of <b>$58</b>, implying 22.9% upside. The combination of "
                "durable software growth, improving unit economics and a net cash balance sheet "
                "offers an attractive risk and reward profile. We would revisit the rating if "
                "subscription attach rates fall below 85% or if gross margin fails to expand in "
                "FY2026."),
              p("Catalysts", H3)]
    story += bullets(["Q3 FY2026 results (November): first quarter with European channel revenue.",
                      "Investor day (February): expected multi-year margin targets.",
                      "Decision on a large grocery distribution programme, expected in the first half of 2027."])
    story.append(PageBreak())

    # ── appendix ─────────────────────────────────────────────────────────────
    story += [p("Appendix: methodology and sources", H2),
              p("Methodology", H3)]
    story += bullets([
        "Historical figures were compiled from the company's annual and quarterly reports and "
        "reconciled to reported totals. Segment splits follow company disclosure.",
        "Forecasts are built bottom up: robots deployed times average selling price for Systems, "
        "robots under management times annual software price for Software and Services.",
        "The DCF uses a ten-year explicit period, mid-year discounting and a Gordon growth "
        "terminal value. WACC assumes a 4.2% risk-free rate, 5.0% equity risk premium, beta "
        "of 1.15 and no debt.",
        "Comparable companies were selected on business model and end market; multiples use "
        "consensus-style FY2026 estimates.",
        "Every figure in the report was checked to tie to its source table before delivery.",
    ])
    story += [p("Sources (illustrative)", H3)]
    story += bullets([
        "Northwind Robotics, Inc. Form 10-K, fiscal years 2023, 2024 and 2025 (illustrative).",
        "Northwind Robotics, Inc. Form 10-Q, Q1 to Q3 fiscal 2025 (illustrative).",
        "Northwind Robotics, Inc. definitive proxy statement, 2025 (illustrative).",
        "Peer company annual reports for the six comparable companies listed (illustrative).",
        "Industry estimates of warehouse automation penetration (illustrative).",
    ])
    story += [Spacer(1, 10), p("Disclosures", H3),
              p("Northwind Robotics, Inc. and every company named in this report are fictional. "
                "All figures are illustrative and were created to demonstrate a research "
                "deliverable. Nothing in this document is investment advice or an offer to buy "
                "or sell any security.", SMALL)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(OUT), pagesize=LETTER, leftMargin=0.8 * inch,
                            rightMargin=0.8 * inch, topMargin=0.85 * inch, bottomMargin=0.9 * inch,
                            title="Northwind Robotics, Inc. (NWRB): Initiation of coverage",
                            author="Ledgerline Financial Analyst", invariant=1)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
