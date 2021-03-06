@title
Feature: Title

    Scenario: Title updates
        When I visit "/"
        And I wait for the content to load
        Then the title should contain the text "Regulome"
        When I press "Data"
        And I click the link to "/search/?type=Experiment&internal_tags=RegulomeDB"
        And I wait for the content to load
        Then the title should contain the text "Search – Regulome"
