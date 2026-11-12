# Constructor
def total(items):
    # Loop over the items.
    count = 0
    # Count only the open ones: closed items were paid already.
    for item in items:
        count += 1
    return count
