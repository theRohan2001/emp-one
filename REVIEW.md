# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | 'list_employees' endpoint(line: 93) | the 'skip' variable computation skips the first page | By testing the endpoint (saw in the Swagger UI) | The starter skips page * page_size items, so page 1 skips the first 20 and never shows them, whereas it should skip (page - 1) * page_size so page 1 skips none |
| 2 | 'punch_in' endpoint | The 'emp' variable that gets the employee document from MongoDB doesn't check if the document exist or not making the app return 500 because an unknown emp_code makes emp equal to None, and the later emp["shift_start"] raises TypeError, which is a 500 and the openapi documentation wants a 404 | During testing the endpoint | Just by adding a check if emp is None return an HTTPException with a 404 and "Employee doesn't exist" detail on line 102-103 |
| 3 | 'EmployeeIn' pydantic data model (line 56-77) | missing field validations on the EmployeeIn pydantic model that are mentioned in the openapi.yaml file | noticed while testing the create_emlpoyee endpoint and reading the openapi.yaml documentation | fixed it by adding the necessary custom validations mentioned in the openapi.yaml file |
| 4 | list_function (line: 107) | no validation on the query parameters of the list_employee function under the GET "/employess" endpoint | noticed after reading the openapi.yaml file | added validation on page and page_size path queries using 'Query' function provided FastAPI
| 5 | line: 30 | no database level check if the same employee has punched in on the date, just checks at the API level, making 'check-then-insert' which is also slow as it checks the whole attendance_logs collection everytime some employee punches in for the day and two simultaneous punch-ins for the same employee and date can both pass the check and both insert, giving two 201s and duplicate records | by testing | added an unique index (emp_code, date) on the attendance_logs collection to avoid duplicate entries by the same employee and punch-in times and for fast lookups without checking the whole attendance_logs collection
| 6 | line-22 |  The PyMongo client isn’t timezone, so datetimes come back offset naive | By testing | Fixed it by adding tz_info and setting it to true
| 7 |  health endpoint | /health always returns ok. It must ping MongoDB and return 503 on failure. | testing | added 'ping' command to check if the connection is available or not
| 8 | list_attendance function | It exposes _id, returns datetimes instead of epoch ms, and doesn’t default missing history and half_day for legacy records. | by testing | added a pydantic responce model validation to fix these returning fields
| 9 | compute_overtime function | It has no 30-minute minimum (R3) and returns any positive minutes. | testing | fixed by only returning the late minutes when they are more or equal to 30 else 0 
| 10 | list_attendance fucntion/endpoint | total is derived from len(docs), and page_size is uncapped. | testing | fixed it by count_docutments function on the attendance_logs collection which is more precise and accurate and added Query field validation on page and page_size
| 11 | punch-in endpoint/function (line - 100) | An unknown employee gives emp = None, then a TypeError and a 500. It should be 404. | testing | fixed it by adding a chech using if statement if the emp is none then raise a 404 HTTP error, not found.
| 12 | punch-in endpoint/function | datetime.fromtimestamp(...) uses the server’s local time zone, and datetime.now() is naive local time. Both are wrong. | testing | fixed it by passing the utc timezone info on both the functions

Also note anything you looked at and decided was **not** a defect, and why.
