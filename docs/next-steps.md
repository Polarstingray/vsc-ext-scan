# UntrustIDE additions
creating additional sources and sinks that test different aspects of a codebase. 
UntrustIDE was only testing for security vulnerabilities that may be exploited by external users.

### our additions
instead of assuming that extensions are benign, we also look for behavior that indicates the extension
itself is the one doing the exploiting. not a vulnerability in the extension, but an explicit feature with
the purpose of attack such as accessing a vs code terminal and running certain shell commands. 


# what is still left
we've created a few additional sources and sinks, but require a method for testing them on a large scale
against actual vs code extensions. that is, we must pull the code for many extensions, turn them into codeql
databases, test our queries against them, and catelog the results in an easy manner for data modeling, graphs, 
discussing results, etc.

### new tool
for this we need a new tool that is capable of doing this automatically. it would be easy enough to do it against the
provided databases that came with the UntrustIDE repo, but for real results we would like to test it against a large
data set of extensions just like they did. the issue is that UntrustIDE did not provide a tool or describe their methods 
of scraping and automatically testing their queries.

# goal
a tool capable of scraping the market place for extensions that can be compiled into codeQL databases, then running our queries against
them, outputting the results in an easy to read format (likely CSV) which will be used to create graphs/figures.