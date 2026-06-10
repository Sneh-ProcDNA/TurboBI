# Calc-field corpus analysis
- **Total calc fields:** 1268
- **Translator drops (returned `None`):** 70 (5.5%)
- **Workbooks scanned:** 18

## Per-workbook calc field counts

| Workbook | Calc fields | Drops |
|---|--:|--:|
| Superstore Shipping Metrics _ #VOTD | 184 | 27 |
| Production Report | 141 | 5 |
| Merchandise Sales Dashboard | 129 | 4 |
| Help Desk _ #RWFD _ VOTD | 119 | 2 |
| Marketing Campaign Performance | 107 | 3 |
| Higher Education Admissions Dashboard _ VOTD | 101 | 11 |
| Site Monitoring | 96 | 3 |
| Account Leads Dashboard | 69 | 2 |
| Healthcare Resources Analysis for National Healthcare Group | 69 | 6 |
| Superstore Performance Dashboard _ #VOTD | 61 | 4 |
| Medical Affairs Dashboard | 56 | 1 |
| Quality Checks | 56 | 1 |
| Sprint Output | 42 | 1 |
| DQM Dashboard | 22 | 0 |
| Birth Control | 10 | 0 |
| UseCase2_Custom | 2 | 0 |
| UseCase2_test 3 | 2 | 0 |
| UseCase2_test | 2 | 0 |

## Per-bucket success rate

| Primary bucket | Count | Drops | Drop rate |
|---|--:|--:|--:|
| `AGG_BASIC` | 325 | 17 | 5% |
| `OTHER` | 296 | 1 | 0% |
| `BLOCK_IF` | 152 | 12 | 8% |
| `LITERAL_ONLY` | 119 | 0 | 0% |
| `LOD_FIXED` | 100 | 1 | 1% |
| `DATE_FN` | 68 | 1 | 1% |
| `BLOCK_CASE` | 56 | 0 | 0% |
| `RANK` | 27 | 21 | 78% |
| `REF_ONLY` | 27 | 0 | 0% |
| `STRING_FN` | 25 | 4 | 16% |
| `WINDOW_AGG` | 21 | 0 | 0% |
| `AGG_ATTR` | 20 | 1 | 5% |
| `TYPE_CAST` | 14 | 0 | 0% |
| `RUNNING_AGG` | 7 | 7 | 100% |
| `FN_IIF` | 5 | 0 | 0% |
| `LOOKUP` | 4 | 4 | 100% |
| `FIRST_LAST` | 1 | 0 | 0% |
| `LOD_INCLUDE` | 1 | 1 | 100% |

## Examples by bucket

### `AGG_BASIC` (325 fields)

**Translated cleanly:**

- *Account Leads Dashboard* — `trends_tivi_pt` (measure/int64)
  - Tableau: `COUNTD(
    IF [PATIENT_AGE] < 17 AND [latest_mpsii_tx_type] = 'AVLAYAH' THEN [PATIENT_ID]
    END
)`
  - DAX: `DISTINCTCOUNT(SWITCH(TRUE(), MIN('pt_360_table'[Patient Age]) < 17 && MIN('pt_360_table'[Latest Mpsii Tx Type]) = "AVLAYAH", MIN('pt_360_table'[Patient Id])))`
- *Account Leads Dashboard* — `trend_elaprase_pt` (measure/int64)
  - Tableau: `COUNTD(
    IF [PATIENT_AGE] < 17 AND [latest_mpsii_tx_type] = 'ELAPRASE' THEN [PATIENT_ID]
    END
)`
  - DAX: `DISTINCTCOUNT(SWITCH(TRUE(), MIN('pt_360_table'[Patient Age]) < 17 && MIN('pt_360_table'[Latest Mpsii Tx Type]) = "ELAPRASE", MIN('pt_360_table'[Patient Id])))`
- *Account Leads Dashboard* — `sp_percentage` (measure/double)
  - Tableau: `IF SUM(
        IF [kpi_name] = 'TOTAL_VIALS' THEN [kpi_value] END
    ) = 0
THEN 0
ELSE
    SUM(
        IF [kpi_name] = 'SP_VIALS' THEN [kpi_value] END
    ) 
    /
    SUM(
        IF [kpi_name] = 'TOTAL_VIALS' THEN [kpi_value] END
    )
END`
  - DAX: `SWITCH(TRUE(), SUM(SWITCH(TRUE(), MIN('vw_reporting_kpi_metrics_master'[Kpi Name]) = "TOTAL_VIALS", MIN('vw_reporting_kpi_metrics_master'[Kpi Value]))) = 0, 0, SUM(SWITCH(TRUE(), MIN('vw_reporting_kpi_metrics_master'[Kpi Name]) = "SP_VIALS", MIN('vw_reporting_kpi_metrics_master'[Kpi Value]))) / SUM(SWITCH(TRUE(), MIN('vw_reporting_kpi_metrics_master'[Kpi Name]) = "TOTAL_VIALS", MIN('vw_reporting_kpi_metrics_master'[Kpi Value]))))`

**Dropped (translator returned `None`):**

- *Account Leads Dashboard* — `trend_mpsii_patients` (measure/int64)
  - Tableau: `COUNTD(
    IF [PATIENT_AGE] >= 17 and [latest_mpsii_tx_type] in ('AVLAYAH', 'ELAPRASE') THEN [PATIENT_ID]
    END
)`
- *Site Monitoring* — `Available Count Alation` (measure/double)
  - Tableau: `IF(MAX([TOTAL_CAPACITY])-COUNTD([USER_EMAIL (ALATION_LICENSE_FINAL)]))< 0 THEN 0 ELSE (MAX([TOTAL_CAPACITY])-COUNTD([USER_EMAIL (ALATION_LICENSE_FINAL)]))/MAX([TOTAL_CAPACITY]) END`
- *Superstore Shipping Metrics _ #VOTD* — `% Change +` (measure/double)
  - Tableau: `IF
(SUM([Calculation_99360729833828364]) - SUM([CY (copy)_99360729834057741]))/SUM([CY (copy)_99360729834057741]) > 0 
THEN
(SUM([Calculation_99360729833828364]) - SUM([CY (copy)_99360729834057741]))/SUM([CY (copy)_99360729834057741])
ELSE NULL
END`

### `OTHER` (296 fields)

**Translated cleanly:**

- *Account Leads Dashboard* — `tab_payer` (dimension/boolean)
  - Tableau: `[Parameters].[Parameter 2] = "PAYER"`
  - DAX: `MIN('Parameters'[Parameter 2]) = "PAYER"`
- *Account Leads Dashboard* — `trend_total_label` (measure/int64)
  - Tableau: `[Calculation_691584062177619969]+[Calculation_553379856035995671]+[Calculation_553379856039534617]`
  - DAX: `MIN('tableau_long_view_pt360'[trend_elaprase_pt (tableau_long_view_pt360)]) + MIN('tableau_long_view_pt360'[trends_tivi_pt (tableau_long_view_pt360)]) + MIN('tableau_long_view_pt360'[trend_mpsii_patients (tableau_long_view_pt360)])`
- *Account Leads Dashboard* — `tab_Care Team_tivi` (dimension/boolean)
  - Tableau: `[Parameters].[Parameter 1] = "Care Team AVLAYAH"`
  - DAX: `MIN('Parameters'[Parameter 1]) = "Care Team AVLAYAH"`

**Dropped (translator returned `None`):**

- *Superstore Shipping Metrics _ #VOTD* — `Year Orders %` (measure/double)
  - Tableau: `ROUND(
([Total Orders (copy)_1559371376287309832]/ [Index+1 Shapex100 (copy)_1559371376286896135])
,2)
*100`

### `BLOCK_IF` (152 fields)

**Translated cleanly:**

- *Account Leads Dashboard* — `tab_bg_payer` (dimension/string)
  - Tableau: `IF [Parameters].[Parameter 2] = "PAYER" THEN "Selected"
ELSE "Unselected"
END`
  - DAX: `SWITCH(TRUE(), MIN('Parameters'[Parameter 2]) = "PAYER", "Selected", "Unselected")`
- *Account Leads Dashboard* — `tab_bg_care_tivi` (dimension/string)
  - Tableau: `IF [Parameters].[Parameter 1] = "Care Team AVLAYAH" THEN "Selected"
ELSE "Unselected"
END`
  - DAX: `SWITCH(TRUE(), MIN('Parameters'[Parameter 1]) = "Care Team AVLAYAH", "Selected", "Unselected")`
- *Account Leads Dashboard* — `tab_bg_comorb` (dimension/string)
  - Tableau: `IF [Parameters].[Parameter 1] = "Comorbidities" THEN "Selected"
ELSE "Unselected"
END`
  - DAX: `SWITCH(TRUE(), MIN('Parameters'[Parameter 1]) = "Comorbidities", "Selected", "Unselected")`

**Dropped (translator returned `None`):**

- *Account Leads Dashboard* — `sort_hcp` (measure/int64)
  - Tableau: `IF([primary_hcp_name_2yr 1]) = 'Unknown' THEN 999999999 
ELSE -[total_mpsii_avlayah_elaprase (vw_rpt_patient_360_hcp_table)]
END`
- *Help Desk _ #RWFD _ VOTD* — `Priority Filter` (dimension/int64)
  - Tableau: `If ([Calculation_1606940709349335044] = 'Open' or [Calculation_1606940709349335044] = 'Waiting') 
   AND [Severity] = 'High' THEN 1

ELSEIF  ([Calculation_1606940709349335044] = 'Open' or [Calculation_1606940709349335044] = 'Waiting')
    AND [Severity] = 'Medium' then 2

ELSEIF ([Ticket Status] = 'Open' OR [Calculation_1606940709349335044] = 'Waiting') 
    AND ([Severity] = 'Low' or [Severity] = 'Unassigned') then 3

else 4 END`
- *Superstore Shipping Metrics _ #VOTD* — `% Change +|- (D)` (measure/string)
  - Tableau: `IF
(([CY Fullfilment Days (copy)_2179824137834508]) - ([PY Fullfilment Days (copy)_2179824137834507]))/([PY Fullfilment Days (copy)_2179824137834507]) > 0 
THEN
'+'
ELSE ''
END`

### `LITERAL_ONLY` (119 fields)

**Translated cleanly:**

- *Account Leads Dashboard* — `pt_by_age_group_header` (dimension/string)
  - Tableau: `"<5yrs | 5-10yrs | 11-18yrs | >18yrs"`
  - DAX: `"<5yrs | 5-10yrs | 11-18yrs | >18yrs"`
- *Account Leads Dashboard* — `hcp_prescribed` (dimension/string)
  - Tableau: `"HCP Prescribed"`
  - DAX: `"HCP Prescribed"`
- *Account Leads Dashboard* — `HCO_Ordered` (dimension/string)
  - Tableau: `"HCO Ordered"`
  - DAX: `"HCO Ordered"`

### `LOD_FIXED` (100 fields)

**Translated cleanly:**

- *Birth Control* — `Max percent` (measure/double)
  - Tableau: `{ FIXED [stopMethod] : MAX([percent])}`
  - DAX: `CALCULATE(MAX('sideEffects.csv'[Percent]), ALLEXCEPT('sideEffects.csv', 'sideEffects.csv'[Stop Method]))`
- *Healthcare Resources Analysis for National Healthcare Group* — `Avg Patient per Doctor for each state` (measure/double)
  - Tableau: `{ FIXED [State]: AVG([Patients per Doctor])}`
  - DAX: `CALCULATE(AVERAGE('Extract'[Patients per Doctor]), ALLEXCEPT('Extract', 'Extract'[State]))`
- *Healthcare Resources Analysis for National Healthcare Group* — `Number of Patients given ICD 10 and Month (1)` (measure/int64)
  - Tableau: `{ FIXED [ICD-10 Code], YEAR([Date of Visit]),MONTH([Date of Visit]): COUNT([PatientID])}`
  - DAX: `CALCULATE(COUNT('Extract'[Patient ID]), ALLEXCEPT('Extract', 'Extract'[ICD-10 Code], 'Extract'[Year of Date of Visit], 'Extract'[Month Number of Date of Visit]))`

**Dropped (translator returned `None`):**

- *Marketing Campaign Performance* — `Metric Rate | L10W* (copy)` (measure/double)
  - Tableau: `IF ([Metric Name]) = 'CPC'
   OR ([Metric Name]) = 'ROI'
   OR ([Metric Name]) = 'Revenue'
   OR ([Metric Name]) = 'Conversion Rate'
   OR ([Metric Name]) = 'Engagement Rate'

THEN 
{ FIXED [Metric Name], [Year | Adjusted], [Week No | Adjusted], [Channel]: SUM([Trend: Metric Denom (Selected Year) (copy)_2588162461158694931])/SUM([Metric Denom (Selected Month) (copy)_2588162461158432785])}

ELSE { FIXED [Metric Name], [Year | Adjusted], [Week No | Adjusted], [Channel]: SUM([Trend: Metric Denom (Selected Year) (copy)_2588162461158694931])}

END`

### `DATE_FN` (68 fields)

**Translated cleanly:**

- *Help Desk _ #RWFD _ VOTD* — `Previous Month` (dimension/boolean)
  - Tableau: `[Date of Request (Months)] = DATEADD("month", -1, [Parameters].[Date of Request (Months) Parameter])`
  - DAX: `'Help Desk RWFD.csv'[Date of Request (Months)] = DATEADD("month",- 1,MIN('Parameters'[Date of Request (Months) Parameter]))`
- *Help Desk _ #RWFD _ VOTD* — `Show Next Month` (dimension/dateTime)
  - Tableau: `DATE(DATEADD('month', 1, [Parameters].[Date of Request (Months) Parameter]))`
  - DAX: `DATEVALUE(DATEADD("month",1,MIN('Parameters'[Date of Request (Months) Parameter])))`
- *Help Desk _ #RWFD _ VOTD* — `Sort by | Table` (measure/int64)
  - Tableau: `CASE [Parameters].[Parameter 3]

WHEN 1 THEN [Days Open]
WHEN 2 THEN -DAY([Date of Request])

END`
  - DAX: `SWITCH(MIN('Parameters'[Parameter 3]), 1, MIN('Help Desk RWFD.csv'[Days Open]), 2, - DAY(MIN('Help Desk RWFD.csv'[Date of Request])))`

**Dropped (translator returned `None`):**

- *Merchandise Sales Dashboard* — `Date Dimension` (dimension/dateTime)
  - Tableau: `DATETRUNC([Parameters].[Parameter 2], [Order Date])`

### `BLOCK_CASE` (56 fields)

**Translated cleanly:**

- *DQM Dashboard* — `Data Quality CF` (measure/double)
  - Tableau: `CASE [Parameters].[Parameter 1]
WHEN "Accuracy" THEN [Calculation_2170735044641333252]
WHEN "Consistency" THEN [Calculation_2170735044641529862]
WHEN "Completeness" THEN [Calculation_2170735044641447941]
WHEN "Timeliness" THEN [Calculation_2170735044641611783]
WHEN "Uniqueness" THEN [Calculation_2170735044641824776]
WHEN "Validity" THEN [Calculation_2170735044641910793]
END`
  - DAX: `SWITCH(MIN('Parameters'[Parameter 1]), "Accuracy", 'Sheet1'[Accuracy Parameter], "Consistency", 'Sheet1'[Consistency Parameter], "Completeness", 'Sheet1'[Completeness Parameter], "Timeliness", 'Sheet1'[Timeliness Parameter], "Uniqueness", 'Sheet1'[Uniqueness Parameter], "Validity", 'Sheet1'[Validity Parameter])`
- *DQM Dashboard* — `Data Validation CF` (measure/double)
  - Tableau: `CASE [Parameters].[Parameter 2]
WHEN "Data Delivery Trend" THEN [Calculation_2170735044640751616]
WHEN "Error Rate" THEN [Calculation_2170735044641103874]
WHEN "Validation Failure Rate" THEN [Calculation_2170735044642017290]
END`
  - DAX: `SWITCH(MIN('Parameters'[Parameter 2]), "Data Delivery Trend", 'Sheet1'[Data Delivery Trend], "Error Rate", 'Sheet1'[Error Rate], "Validation Failure Rate", 'Sheet1'[Validation Failure Rate])`
- *Help Desk _ #RWFD _ VOTD* — `Seperator  Label` (dimension/string)
  - Tableau: `CASE [Parameters].[Parameter 1]

WHEN 1 THEN ''
WHEN 2 THEN '|'
WHEN 3 THEN '|'
WHEN 4 THEN ''
WHEN 5 THEN '|'

END`
  - DAX: `SWITCH(MIN('Parameters'[Parameter 1]), 1, "", 2, "|", 3, "|", 4, "", 5, "|")`

### `RANK` (27 fields)

**Translated cleanly:**

- *Marketing Campaign Performance* — `Channel | Rank | Display | No icons` (measure/double)
  - Tableau: `IF ATTR([Channel Filter (copy)_1834090984248467465]) THEN [Channel Rank (copy)_1834090984250335245] END`
  - DAX: `SWITCH(TRUE(), SELECTEDVALUE('MarketingCampaign Aggregated'[Channel Filter | No Icons]), 'MarketingCampaign Aggregated'[Channel Rank | No Icons])`
- *Merchandise Sales Dashboard* — `Sort by Dimension` (measure/int64)
  - Tableau: `CASE [Parameters].[Parameter 5]
WHEN 'Highest Value' THEN RANK(SUM([Calculation_254383022497943554]),'desc')
WHEN 'Highest Value' THEN RANK(SUM([Calculation_254383022497943554]),'asc')
WHEN 'Latest Date' THEN RANK(MIN(INT(DATETRUNC('day',[Order Date]))),'desc')
END`
  - DAX: `SWITCH(MIN('Parameters'[Parameter 5]), "Highest Value", RANKX(ALLSELECTED('federated.180hlsq1gpr1se17h99a019xk5ox'), SUM('federated.180hlsq1gpr1se17h99a019xk5ox'[Calculation_254383022497943554]), , DESC, Skip), "Highest Value", RANKX(ALLSELECTED('federated.180hlsq1gpr1se17h99a019xk5ox'), SUM('federated.180hlsq1gpr1se17h99a019xk5ox'[Calculation_254383022497943554]), , ASC, Skip), "Latest Date", RANKX(ALLSELECTED('federated.180hlsq1gpr1se17h99a019xk5ox'), MIN(INT(INT(MIN('federated.180hlsq1gpr1se17h99a019xk5ox'[Order Date])))), , DESC, Skip))`
- *Merchandise Sales Dashboard* — `Rank` (measure/boolean)
  - Tableau: `RANK([LinPack_628373556243785611])<=1`
  - DAX: `RANKX(ALLSELECTED('federated.180hlsq1gpr1se17h99a019xk5ox'), MIN('federated.180hlsq1gpr1se17h99a019xk5ox'[LinPack_628373556243785611]), , DESC, Skip) <= 1`

**Dropped (translator returned `None`):**

- *Healthcare Resources Analysis for National Healthcare Group* — `Index` (measure/int64)
  - Tableau: `INDEX()`
- *Healthcare Resources Analysis for National Healthcare Group* — `Index` (measure/int64)
  - Tableau: `INDEX()`
- *Healthcare Resources Analysis for National Healthcare Group* — `Index` (measure/int64)
  - Tableau: `INDEX()`

### `REF_ONLY` (27 fields)

**Translated cleanly:**

- *Account Leads Dashboard* — `Latest Mpsii Tx Type-KPI` (dimension/string)
  - Tableau: `[latest_mpsii_tx_type]`
  - DAX: `MIN('pt_360_table'[Latest Mpsii Tx Type])`
- *Birth Control* — `Reason Condom` (dimension/int64)
  - Tableau: `[reason]`
  - DAX: `MIN('sideEffects.csv'[Reason])`
- *Birth Control* — `Reason  Injection` (dimension/int64)
  - Tableau: `[reason]`
  - DAX: `MIN('sideEffects.csv'[Reason])`

### `STRING_FN` (25 fields)

**Translated cleanly:**

- *Birth Control* — `Efficacy - Split 1` (measure/int64)
  - Tableau: `INT(TRIM( SPLIT( [Efficacy], "%", 1 ) ))`
  - DAX: `INT(TRIM(PATHITEM(SUBSTITUTE(MIN('everused.csv'[Efficacy]), "%", "|"), 1)))`
- *Healthcare Resources Analysis for National Healthcare Group* — `Reason for visit - Split 1` (dimension/string)
  - Tableau: `TRIM( SPLIT( [Reason for visit], " ", 1 ) )`
  - DAX: `TRIM(PATHITEM(SUBSTITUTE(MIN('Extract (9)'[Reason for visit]), " ", "|"), 1))`
- *Healthcare Resources Analysis for National Healthcare Group* — `Reason for visit - Split 2` (dimension/string)
  - Tableau: `TRIM( SPLIT( [Reason for visit], " ", 2 ) )`
  - DAX: `TRIM(PATHITEM(SUBSTITUTE(MIN('Extract (9)'[Reason for visit]), " ", "|"), 2))`

**Dropped (translator returned `None`):**

- *Production Report* — `Sprint Number 1` (measure/int64)
  - Tableau: `INT(REGEXP_EXTRACT([SPRINT], '(\d+)'))`
- *Production Report* — `Sprint No.` (measure/int64)
  - Tableau: `INT(REGEXP_EXTRACT([CURRENT_SPRINT], '(\d+)'))`
- *Production Report* — `Sprint Dim No.` (measure/int64)
  - Tableau: `INT(REGEXP_EXTRACT([SPRINT_NAME], '(\d+)'))`

### `WINDOW_AGG` (21 fields)

**Translated cleanly:**

- *DQM Dashboard* — `%of GT Max Value DQ` (measure/double)
  - Tableau: `MAX([Value-DQ]) / 
WINDOW_SUM(MAX([Value-DQ]))`
  - DAX: `MAX('Sheet3'[Value-DQ]) / CALCULATE(SUM('Sheet3'[Value-DQ]), ALLSELECTED('Sheet3'))`
- *Help Desk _ #RWFD _ VOTD* — `Cap Ticket Bar Charts` (measure/double)
  - Tableau: `WINDOW_MAX([Calculation_1527846238267117637])`
  - DAX: `CALCULATE(MAX('Help Desk RWFD.csv'[CM Selected p Metric]), ALLSELECTED('Help Desk RWFD.csv'))`
- *Help Desk _ #RWFD _ VOTD* — `Cap ADO bar charts` (measure/double)
  - Tableau: `WINDOW_MAX(AVG([Days Open]))+5`
  - DAX: `CALCULATE(MAX('Help Desk RWFD.csv'[Days Open]), ALLSELECTED('Help Desk RWFD.csv')) + 5`

### `AGG_ATTR` (20 fields)

**Translated cleanly:**

- *Higher Education Admissions Dashboard _ VOTD* — `Curve 1-2 Max` (measure/double)
  - Tableau: `[Calculation_8950329121202785] + (([Calculation_5790329121624550] - [Calculation_8950329121202785]) * ATTR([Calculation_2810329120811267]))`
  - DAX: `MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Calculation_8950329121202785]) + ((MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Calculation_5790329121624550]) - MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Calculation_8950329121202785])) * SELECTEDVALUE('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Calculation_2810329120811267]))`
- *Higher Education Admissions Dashboard _ VOTD* — `Curve 1-2 Polygon` (measure/double)
  - Tableau: `CASE ATTR([Min or Max])

WHEN 'Min' THEN [Calculation_8830329122759423]
WHEN 'Max' THEN [Calculation_1490329122549987]

END`
  - DAX: `SWITCH(SELECTEDVALUE('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Min or Max]), "Min", MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Calculation_8830329122759423]), "Max", MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Calculation_1490329122549987]))`
- *Higher Education Admissions Dashboard _ VOTD* — `Reference Line` (measure/double)
  - Tableau: `IF ATTR([Application Date (Years)])<= #2023-01-01# THEN 0.75 END`
  - DAX: `SWITCH(TRUE(), SELECTEDVALUE('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Application Date (Years)]) <= # 2023 - 01 - 01 #, 0.75)`

**Dropped (translator returned `None`):**

- *Site Monitoring* — `WhizAI Capacity %` (measure/double)
  - Tableau: `IF 
(ATTR([CURRENT_LICENCE_CAPACITY]) - [WhizAI Liceanced Analyst Count (copy)_946881857632911369])/ATTR([CURRENT_LICENCE_CAPACITY]) < 0
THEN 0
ELSE
(ATTR([CURRENT_LICENCE_CAPACITY]) - [WhizAI Liceanced Analyst Count (copy)_946881857632911369])/ATTR([CURRENT_LICENCE_CAPACITY])
END`

### `TYPE_CAST` (14 fields)

**Translated cleanly:**

- *Higher Education Admissions Dashboard _ VOTD* — `Application Round` (dimension/string)
  - Tableau: `STR([Application Period Start Date])+"-"+STR([Application Period End Date])`
  - DAX: `FORMAT(MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Application Period Start Date]), "") & "-" & FORMAT(MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Application Period End Date]), "")`
- *Higher Education Admissions Dashboard _ VOTD* — `ID table` (dimension/string)
  - Tableau: `STR([Application Date])+
STR([Applicant Id])+
[Age Band]+
[Program - Split 1]+
[Program - Split 2]+
[Nationality]+
[Application Status]+
[Scholarship Applicant]+
[Valid Application Flag]`
  - DAX: `FORMAT(MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Application Date]), "") + FORMAT(MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Applicant Id]), "") + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Age Band]) + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Program - Split 1]) + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Program - Split 2]) + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Nationality]) + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Application Status]) + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Scholarship Applicant]) + MIN('federated.0rdon331r0s0vi1b5az8y1df2ouw'[Valid Application Flag])`
- *Merchandise Sales Dashboard* — `Filter Date` (dimension/boolean)
  - Tableau: `[Order Date] >= [Parameters].[Parameter 4] 
AND 
[Order Date] <= [Parameters].[Start Date (copy)_197736184057262135]`
  - DAX: `MIN('federated.180hlsq1gpr1se17h99a019xk5ox'[Order Date]) >= MIN('Parameters'[Parameter 4]) && MIN('federated.180hlsq1gpr1se17h99a019xk5ox'[Order Date]) <= MIN('Parameters'[Start Date (copy)_197736184057262135])`

### `RUNNING_AGG` (7 fields)


**Dropped (translator returned `None`):**

- *Higher Education Admissions Dashboard _ VOTD* — `N1 Bar Position` (measure/double)
  - Tableau: `RUNNING_SUM([Calculation_1490329120836378 1]+ [Calculation_604889763570143232]) - [Calculation_1490329120836378 1] - [Calculation_604889763570143232]/2`
- *Higher Education Admissions Dashboard _ VOTD* — `N2 Position` (measure/double)
  - Tableau: `RUNNING_SUM([N1 Flow Size (copy)]) - [N1 Flow Size (copy)] + [Calculation_2320329132122533]*[N1 Whitespace (copy)] - [N1 Whitespace (copy)]/2`
- *Higher Education Admissions Dashboard _ VOTD* — `N2 Bar Position` (measure/double)
  - Tableau: `RUNNING_SUM([N1 Flow Size (copy)]+ [N1 Whitespace (copy)]) - [N1 Flow Size (copy)] - [N1 Whitespace (copy)]/2`

### `FN_IIF` (5 fields)

**Translated cleanly:**

- *Merchandise Sales Dashboard* — `Toggle Button` (dimension/boolean)
  - Tableau: `IIF([Parameters].[Parameter 3]=False,True,False)`
  - DAX: `IF(MIN('Parameters'[Parameter 3]) = FALSE(), TRUE(), FALSE())`
- *Superstore Shipping Metrics _ #VOTD* — `Placement` (measure/int64)
  - Tableau: `IIF([Calculation_3314086408456491009]='CY',2,1)`
  - DAX: `IF('Orders'[CY or PY] = "CY", 2, 1)`
- *Superstore Shipping Metrics _ #VOTD* — `Placement Year` (measure/int64)
  - Tableau: `IIF([Calculation_99360729557819395]=2024,4,IIF([Calculation_99360729557819395]=2023,3,IIF([Calculation_99360729557819395]=2022,2,1)))`
  - DAX: `IF('Orders'[Year] = 2024, 4, IF('Orders'[Year] = 2023, 3, IF('Orders'[Year] = 2022, 2, 1)))`

### `LOOKUP` (4 fields)


**Dropped (translator returned `None`):**

- *Superstore Performance Dashboard _ #VOTD* — `YoY Sales` (measure/double)
  - Tableau: `(SUM([Sales]) - LOOKUP(SUM([Sales]), -1)) / LOOKUP(SUM([Sales]), -1)`
- *Superstore Performance Dashboard _ #VOTD* — `YoY Customers` (measure/double)
  - Tableau: `([Calculation_6049474640629761] - LOOKUP([Calculation_6049474640629761], -1)) / LOOKUP([Calculation_6049474640629761], -1)`
- *Superstore Performance Dashboard _ #VOTD* — `YoY Orders` (measure/double)
  - Tableau: `(COUNTD([Order ID]) - LOOKUP(COUNTD([Order ID]), -1)) / LOOKUP(COUNTD([Order ID]), -1)`

### `FIRST_LAST` (1 fields)

**Translated cleanly:**

- *Superstore Performance Dashboard _ #VOTD* — `Last` (measure/boolean)
  - Tableau: `LAST()=0`
  - DAX: `LAST() = 0`

### `LOD_INCLUDE` (1 fields)


**Dropped (translator returned `None`):**

- *Help Desk _ #RWFD _ VOTD* — `Table Count Tickets` (measure/int64)
  - Tableau: `{INCLUDE: COUNT([__tableau_internal_object_id__].[Help Desk RWFD.csv_1D7D1BC1FB8D498881BF5C583984D64E])}`

## Co-occurring patterns (secondary buckets)

| Bucket | Count |
|---|--:|
| `BLOCK_IF` | 200 |
| `AGG_BASIC` | 133 |
| `TYPE_CAST` | 59 |
| `DATE_FN` | 47 |
| `FN_IF` | 30 |
| `BLOCK_CASE` | 25 |
| `STRING_FN` | 22 |
| `FN_IIF` | 8 |
| `AGG_ATTR` | 2 |
| `RANK` | 1 |

