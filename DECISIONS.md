# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.

A. I created unique indexes on `emp_code` and `(emp_code, date)` for uniqueness and fast lookups. Compound indexes on `(date, emp_code)`, `(emp_code, punch_in)`, and `(department, joined_on)` support specific filtering, sorting, attendance, punch-out, and summary queries. I considered a separate status index but rejected it because `(date, emp_code)` index was sufficient.

B. Two identical punch-in requests arrive at the same instant, one request gets a 201 and the other request(or the loser of the race)  gets a 409, DuplicateKeyError from PyMongoDB client because I have create an unique index on `(emp_code, date)`.

C. ---

D. My department summary endpoint makes sure the employees with zero logs are counted by getting the headcount from the `employees` collection `joined_on` is on or before the last day of the month.

E. If this had to serve 100x the data, I would introduce aysnc MongoDB driver and database sharding.
Async I/O would improve request concurrency, while sharding would distribute the larger attendance logs data and database load across multiple MongoDB nodes.
